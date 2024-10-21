from django.db.models import F
from schedule import Scheduler
import threading
import time
from .kasabulb.kasabulb import Kasa

from .models import Bulb

def run_continuously(self, interval=1):
    """Continuously run, while executing pending jobs at each elapsed
    time interval.
    @return cease_continuous_run: threading.Event which can be set to
    cease continuous run.
    Please note that it is *intended behavior that run_continuously()
    does not run missed jobs*. For example, if you've registered a job
    that should run every minute and you set a continuous run interval
    of one hour then your job won't be run 60 times at each interval but
    only once.
    """

    cease_continuous_run = threading.Event()

    class ScheduleThread(threading.Thread):

        @classmethod
        def run(cls):
            while not cease_continuous_run.is_set():
                self.run_pending()
                time.sleep(interval)

    continuous_thread = ScheduleThread()
    continuous_thread.setDaemon(True)
    continuous_thread.start()
    return cease_continuous_run

Scheduler.run_continuously = run_continuously

from stupidArtnet import StupidArtnetServer

def rx_something(data):
    #start_time = time.time()
    for bulb in Bulb.objects.all():
        if bulb.enabled == False:
            continue
        #print(bulb.name)
        channel = bulb.channel
        hue, sat, val = Kasa.scale_hsv(data[channel-1]-1, data[channel], data[channel+1])
        #print(bulb.ip_addr, channel, hue, sat, val)
        Kasa.change_color(bulb.ip_addr, hue, sat, val)
    #stop_time = time.time()
    #print("--- %s seconds ---" % (stop_time - start_time))

def do_something():
    a = StupidArtnetServer()
    a.register_listener(universe=0, callback_function=rx_something)
    while True:
        time.sleep(100)




def start_scheduler():
    scheduler = Scheduler()
    scheduler.every().second.do(do_something)
    scheduler.run_continuously()