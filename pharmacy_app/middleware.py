import secrets

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden, JsonResponse


class LoginRequiredMiddleware:
    """Require the configured owner account for every application page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        static_root = settings.STATIC_URL
        if not static_root.startswith("/"):
            static_root = "/" + static_root
        public_prefixes = (
            settings.LOGIN_URL.rstrip("/"),
            "/admin",
            static_root.rstrip("/"),
            "/manifest.json",
            "/sw.js",
            "/accounts/logout",
        )
        path = request.path_info
        is_public = any(path == prefix or path.startswith(prefix + "/") for prefix in public_prefixes)

        if not request.user.is_authenticated and not is_public:
            if request.headers.get("X-Offline-Sync") == "1":
                return JsonResponse(
                    {"success": False, "error": "authentication_required"},
                    status=401,
                )
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)

        if request.user.is_authenticated and path not in {"/accounts/logout", "/accounts/logout/"}:
            owner_username = settings.APP_OWNER_USERNAME
            if not owner_username or request.user.get_username() != owner_username:
                return HttpResponseForbidden("This account is not authorized to use this app.")

        response = self.get_response(request)
        if request.user.is_authenticated:
            response["Cache-Control"] = "private, no-store, max-age=0"
            response["Pragma"] = "no-cache"
            response["Vary"] = "Cookie"
        return response


class ContentSecurityPolicyMiddleware:
    """Generate a per-response nonce for inline application scripts."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        nonce = secrets.token_urlsafe(18)
        request.csp_nonce = nonce
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html"):
            response["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com data:; "
                "img-src 'self' data:; connect-src 'self'; worker-src 'self'; "
                "manifest-src 'self'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'"
            )
        return response
