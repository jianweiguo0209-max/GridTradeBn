"""FastAPI 应用工厂：登录鉴权 + 四个只读视图。web 进程绝不写库/写交易所。"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
               stale_threshold_sec: float = 30.0) -> FastAPI:
    app = FastAPI()
    throttle = throttle or LoginThrottle()
    templates = Jinja2Templates(directory=str(_DIR / 'templates'))
    for name, func in (('ms_to_human', fmt.ms_to_human), ('age_human', fmt.age_human),
                       ('fmt_num', fmt.fmt_num), ('fmt_pct', fmt.fmt_pct),
                       ('fmt_size', fmt.fmt_size), ('pnl_class', fmt.pnl_class)):
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

    @app.get('/history', response_class=HTMLResponse)
    def history(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        return templates.TemplateResponse(request, 'history.html', {
            'health': _health(), 'r': build_records(store)})

    return app
