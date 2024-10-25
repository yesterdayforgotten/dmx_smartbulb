from django.urls import path
from config import views
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

urlpatterns = [
    path("", views.home, name="home"),
    path("query/<int:bulb_id>/", views.bulb_query),
    path("set/<int:bulb_id>/h<int:h>s<int:s>v<int:v>/", views.bulb_set),
    path("set/<int:bulb_id>/t<int:t>v<int:v>/", views.bulb_set_temp),
    path("set_default/<int:bulb_id>/t<int:t>v<int:v>/", views.bulb_set_default_state)
]

urlpatterns += staticfiles_urlpatterns()