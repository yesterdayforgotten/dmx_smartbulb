from django.apps import AppConfig
import os

class ConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'config'

    def ready(self):
       from . import jobs

       if os.environ.get('RUN_MAIN', None) != 'true':
           print("RUN_MAIN")
           jobs.start_scheduler()
