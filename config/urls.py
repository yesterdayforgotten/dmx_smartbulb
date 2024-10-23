from django.urls import path
from config import views
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

urlpatterns = [
    path("", views.home, name="home"),
    path("query/<int:bulb_id>/", views.bulb_query),
    path("set/<int:bulb_id>/h<int:h>s<int:s>v<int:v>/", views.bulb_set)
]

urlpatterns += staticfiles_urlpatterns()