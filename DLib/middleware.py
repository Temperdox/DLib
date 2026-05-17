"""DLib middleware — only carries the CORS shim for the public /api/v1/ surface."""
from django.http import HttpResponse


PUBLIC_API_PREFIX = '/api/v1/'
ALLOWED_METHODS = 'GET, POST, OPTIONS'
ALLOWED_HEADERS = 'Content-Type, X-Requested-With'


class PublicApiCorsMiddleware:
    """Permissive CORS for the public /api/v1/ surface.

    The app is a local-only server (binds 127.0.0.1) and the API exposes only
    read-only game metadata, so wildcard origins are fine for personal use.
    Browser extensions / userscripts running on f95zone.to / dlsite.com need
    these headers to read the response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (request.method == 'OPTIONS'
                and request.path.startswith(PUBLIC_API_PREFIX)):
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if request.path.startswith(PUBLIC_API_PREFIX):
            response['Access-Control-Allow-Origin'] = '*'
            response['Access-Control-Allow-Methods'] = ALLOWED_METHODS
            response['Access-Control-Allow-Headers'] = ALLOWED_HEADERS
            response['Access-Control-Max-Age'] = '600'
        return response
