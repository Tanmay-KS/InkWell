"""Vercel entrypoint: the public demo of the InkWell web view.

Vercel's Python runtime looks for an `app` callable in `app.py`. It is the
same handler, routes and static files as `make demo-web`, wrapped in WSGI
by `interface/web/wsgi.py`. The state is fictional and throwaway; see that
module for what is deliberately different from the loopback server.
"""

from interface.web.wsgi import app

__all__ = ["app"]
