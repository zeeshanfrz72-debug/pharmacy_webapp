from django.conf import settings
from django.shortcuts import redirect

class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        exempt_urls = [
            settings.LOGIN_URL,
            '/accounts/signup/',
            '/admin/',
            settings.STATIC_URL if settings.STATIC_URL.startswith('/') else '/' + settings.STATIC_URL,
            '/manifest.json',
            '/sw.js',
        ]
        
        path = request.path_info
        
        if not request.user.is_authenticated:
            is_exempt = any(path.startswith(url) for url in exempt_urls)
            if not is_exempt:
                return redirect(f"{settings.LOGIN_URL}?next={request.path}")
                
        return self.get_response(request)
