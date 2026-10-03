"""
URL configuration for pharmacy_app project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView, RedirectView


urlpatterns = [
    path('', RedirectView.as_view(url='/ledger/add-transaction/', permanent=False)),
    path('sw.js', TemplateView.as_view(template_name="sw.js", content_type="application/javascript"), name="sw.js"),
    path('manifest.json', TemplateView.as_view(template_name="manifest.json", content_type="application/json"), name="manifest.json"),
    path('admin/', admin.site.urls),
    path('ledger/', include('ledger.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
]
