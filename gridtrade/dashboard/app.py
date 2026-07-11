"""FastAPI 应用工厂：登录鉴权 + 四个只读视图。web 进程绝不写库/写交易所。"""
import functools
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gridtrade.dashboard import formatting as fmt
from gridtrade.dashboard.auth import (LoginThrottle, make_session,
                                      verify_password, verify_session)
from gridtrade.dashboard.queries import (build_grid_detail, build_health,
                                         build_overview, build_records)

_DIR = Path(__file__).parent
_COOKIE = 'gt_session'


def create_app(store, adapter, *, username: str, password_hash: str,
               session_secret: str, throttle: Optional[LoginThrottle] = None,
               stale_threshold_sec: float = 30.0,
               flags=None, commands=None, audit=None,
               compute_fn=None, universe_fn=None, display_tz: str = 'UTC') -> FastAPI:
    from gridtrade.state.control import (ControlFlagRepository, CommandRepository,
                                         AuditRepository)
    flags = flags or ControlFlagRepository(store)
    commands = commands or CommandRepository(store)
    audit = audit or AuditRepository(store)
    app = FastAPI()
    throttle = throttle or LoginThrottle()
    templates = Jinja2Templates(directory=str(_DIR / 'templates'))
    for name, func in (('ms_to_human', functools.partial(fmt.ms_to_human, tz_name=display_tz)),
                       ('age_human', fmt.age_human),
                       ('fmt_num', fmt.fmt_num), ('fmt_pct', fmt.fmt_pct),
                       ('fmt_size', fmt.fmt_size), ('fmt_fee', fmt.fmt_fee),
                       ('fmt_price', fmt.fmt_price), ('pnl_class', fmt.pnl_class),
                       ('fill_line', fmt.fill_line)):
        templates.env.filters[name] = func
    app.mount('/static', StaticFiles(directory=str(_DIR / 'static')), name='static')

    def _user(request: Request) -> Optional[str]:
        tok = request.cookies.get(_COOKIE)
        return verify_session(tok, session_secret) if tok else None

    def _health():
        return build_health(store, adapter, stale_threshold_sec=stale_threshold_sec)

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request):
        if _user(request):                       # 已登录 -> 直接回首页
            return RedirectResponse('/', status_code=302)
        return templates.TemplateResponse(request, 'login.html', {'error': None})

    @app.post('/login')
    def login(request: Request, username_in: str = Form(alias='username'),
              password: str = Form(...)):
        if throttle.is_locked(username_in):
            return templates.TemplateResponse(
                request, 'login.html', {'error': 'account locked'},
                status_code=429)
        if username_in == username and verify_password(password, password_hash):
            throttle.record_success(username_in)
            resp = RedirectResponse('/', status_code=302)
            resp.set_cookie(_COOKIE, make_session(username_in, session_secret),
                            httponly=True, samesite='lax', secure=True)
            return resp
        throttle.record_failure(username_in)
        return templates.TemplateResponse(
            request, 'login.html', {'error': 'invalid credentials'},
            status_code=401)

    @app.get('/', response_class=HTMLResponse)
    def overview(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        return templates.TemplateResponse(request, 'overview.html', {
            'health': _health(), 'rows': build_overview(store, adapter)})

    @app.get('/grid/{grid_id}', response_class=HTMLResponse)
    def detail(request: Request, grid_id: str):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        dto = build_grid_detail(store, grid_id)
        if dto is None:
            return HTMLResponse('grid not found', status_code=404)
        return templates.TemplateResponse(request, 'detail.html', {
            'health': _health(), 'd': dto})

    @app.get('/grid/{grid_id}/chart', response_class=HTMLResponse)
    def grid_chart(request: Request, grid_id: str, window: str = 'life'):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        from gridtrade.dashboard import gridchart as gc
        dto = gc.build_grid_chart(store, adapter, grid_id, window)
        if dto is None:
            return HTMLResponse('grid not found', status_code=404)
        return HTMLResponse(gc.render(dto, tz_name=display_tz))

    @app.get('/history', response_class=HTMLResponse)
    def history(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        return templates.TemplateResponse(request, 'history.html', {
            'health': _health(), 'r': build_records(store)})

    @app.post('/control/scheduler')
    def control_scheduler(request: Request, action: str = Form(...)):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        paused = action == 'pause'
        flags.set('scheduler_paused', paused, actor=u)
        audit.add(u, 'FLAG_SET', 'scheduler_paused',
                  detail=json.dumps({'value': paused}))
        return RedirectResponse('/controls', status_code=302)

    @app.post('/control/halt')
    def control_halt(request: Request, action: str = Form(...)):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        on = action == 'on'
        flags.set('trading_halted', on, actor=u)
        audit.add(u, 'FLAG_SET', 'trading_halted',
                  detail=json.dumps({'value': on}))
        return RedirectResponse('/controls', status_code=302)

    @app.post('/control/panic')
    def control_panic(request: Request, confirm: str = Form('')):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        if confirm != 'PANIC':
            return RedirectResponse('/controls?err=confirm', status_code=302)
        flags.set('trading_halted', True, actor=u)
        cmd = commands.enqueue('PANIC_CLOSE_ALL', '{"reason": "panic"}', created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id, detail='{"type": "PANIC_CLOSE_ALL"}')
        return RedirectResponse('/controls', status_code=302)

    @app.post('/control/close')
    def control_close(request: Request, grid_id: str = Form(...),
                      symbol: str = Form(...), reason: str = Form('manual')):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        payload = json.dumps({'grid_id': grid_id, 'symbol': symbol, 'reason': reason})
        cmd = commands.enqueue('CLOSE_GRID', payload, created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id, detail='{"type": "CLOSE_GRID"}')
        return RedirectResponse('/controls', status_code=302)

    @app.get('/open', response_class=HTMLResponse)
    def open_form(request: Request, symbol: str = ''):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        prefill = compute_fn(symbol) if (symbol and compute_fn) else None
        return templates.TemplateResponse(request, 'open.html',
                                          {'symbol': symbol, 'prefill': prefill})

    @app.post('/control/resolve-intervention')
    def control_resolve_intervention(request: Request, symbol: str = Form(...)):
        # 外部干预熔断恢复按钮(spec 2026-07-12 组件三,resolve 形态=用户定):
        # 走指令队列(monitor 落旗),与 CLOSE/OPEN 同一写路径,web 保持零直改执行面。
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        cmd = commands.enqueue('RESOLVE_INTERVENTION',
                               json.dumps({'symbol': symbol}), created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id,
                  detail=json.dumps({'type': 'RESOLVE_INTERVENTION', 'symbol': symbol}))
        return RedirectResponse('/controls', status_code=302)

    @app.get('/controls', response_class=HTMLResponse)
    def controls_page(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        from gridtrade.runtime.commands import INTERVENTION_PREFIX
        braked = sorted(n[len(INTERVENTION_PREFIX):]
                        for n in flags.list_true(INTERVENTION_PREFIX))
        return templates.TemplateResponse(request, 'controls.html', {
            'halted': flags.get('trading_halted'),
            'scheduler_paused': flags.get('scheduler_paused'),
            'interventions': braked,
            'commands': commands.list_recent(), 'audit': audit.list_recent()})

    @app.get('/universe', response_class=HTMLResponse)
    def universe_page(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        rows = universe_fn() if universe_fn else []
        return templates.TemplateResponse(request, 'universe.html', {'rows': rows})

    @app.post('/open')
    def open_submit(request: Request, symbol: str = Form(...),
                    low_price: float = Form(...), high_price: float = Form(...),
                    grid_count: int = Form(...), stop_low_price: float = Form(...),
                    stop_high_price: float = Form(...), cap: str = Form(''),
                    tag: str = Form('gt0'), offset: int = Form(0)):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        params = {'low_price': low_price, 'high_price': high_price,
                  'grid_count': grid_count, 'stop_low_price': stop_low_price,
                  'stop_high_price': stop_high_price}
        body = {'symbol': symbol, 'params': params, 'tag': tag, 'offset': offset}
        if cap.strip():
            body['cap'] = float(cap)
        cmd = commands.enqueue('OPEN_GRID', json.dumps(body), created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id, detail='{"type": "OPEN_GRID"}')
        return RedirectResponse('/controls', status_code=302)

    @app.get('/analytics', response_class=HTMLResponse)
    def analytics_page(request: Request, range: str = 'all', start: str = '', end: str = ''):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        from gridtrade.dashboard import analytics as an
        from gridtrade.dashboard import charts as ch
        from gridtrade.dashboard import export_csv as ex
        from gridtrade.state.models import now_ms
        # 手动日期范围（YYYY-MM-DD，UTC 日界，与 CSV 导出同口径）优先于预设周期
        start, end = start.strip(), end.strip()
        end_ms = None
        if start or end:
            try:
                start_ms = ex.parse_day_ms(start) if start else 0
                end_ms = ex.parse_day_ms(end, end=True) if end else None
            except ValueError:
                return PlainTextResponse('start/end 需为 YYYY-MM-DD', status_code=400)
            if end_ms is not None and start_ms > end_ms:
                return PlainTextResponse('start 不能晚于 end', status_code=400)
            range = '%s ~ %s' % (start or '…', end or '…')
        else:
            cutoff = {'7d': 7 * 86400_000, '30d': 30 * 86400_000}.get(range, 0)
            start_ms = (now_ms() - cutoff) if cutoff else 0
        realized = an.realized_curve(store, start_ms=start_ms, end_ms=end_ms)
        dist = an.fill_distribution(store, start_ms=start_ms, end_ms=end_ms)
        ctx = {
            'range': range,
            'sel_start': start,
            'sel_end': end,
            'equity_svg': ch.line_chart([realized], x_is_time=True,
                                        series_labels=[('#6cf', '已实现')],
                                        value_labels=True, tz_name=display_tz),
            'tags': an.tag_attribution(store, start_ms=start_ms, end_ms=end_ms),
            'by_hour_svg': ch.bar_chart([(str(h), n) for h, n in dist.by_hour], value_labels=True),
            'by_side_svg': (ch.stacked_bar([('成交', dist.by_side)],
                                           seg_labels=[('#4caf50', '买'), ('#e53935', '卖')])
                            if dist.by_side else ch.bar_chart([])),
            'by_line_svg': ch.bar_chart([(str(li), n) for li, n in dist.by_line], value_labels=True),
            'fee_cum_svg': ch.line_chart([dist.fee_cum], x_is_time=True,
                                         series_labels=[('#6cf', '累计手续费')], value_labels=True,
                                         tz_name=display_tz),
            'exits': an.exit_reason_stats(store, start_ms=start_ms, end_ms=end_ms),
        }
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).date()
        ctx['export_start'] = (today - timedelta(days=30)).isoformat()
        ctx['export_end'] = today.isoformat()
        return templates.TemplateResponse(request, 'analytics.html', ctx)

    def _export_csv(request: Request, start: str, end: str, kind: str):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        from gridtrade.dashboard import export_csv as ex
        try:
            s, e = ex.parse_day_ms(start), ex.parse_day_ms(end, end=True)
        except ValueError:
            return PlainTextResponse('start/end 需为 YYYY-MM-DD', status_code=400)
        if s > e:
            return PlainTextResponse('start 不能晚于 end', status_code=400)
        body = ex.grids_csv(store, s, e) if kind == 'grids' else ex.fills_csv(store, s, e)
        return PlainTextResponse(body, media_type='text/csv; charset=utf-8', headers={
            'Content-Disposition': 'attachment; filename="%s_%s_%s.csv"' % (kind, start, end)})

    @app.get('/analytics/export/grids.csv')
    def export_grids(request: Request, start: str = '', end: str = ''):
        return _export_csv(request, start, end, 'grids')

    @app.get('/analytics/export/fills.csv')
    def export_fills(request: Request, start: str = '', end: str = ''):
        return _export_csv(request, start, end, 'fills')

    return app
