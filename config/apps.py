from django.apps import AppConfig
import os
import psutil

class ConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'config'

    def ready(self):
       from . import jobs

       if os.environ.get('RUN_MAIN', None) != 'true':
           print("RUN_MAIN")
           jobs.start_background_processes()
           procs = psutil.Process().children()
           print(procs, flush=True)

