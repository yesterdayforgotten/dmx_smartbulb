from django.contrib import admin

from config.models import Bulb

class KasaAdmin(admin.ModelAdmin):
    pass

admin.site.register(Bulb, KasaAdmin)
