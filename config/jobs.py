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

# from stupidArtnet import StupidArtnetServer
# def rx_something(data):
#     if rx_something.last_update != None and time.time()-rx_something.last_update < .05:
#         return
#     #start_time = time.time()
#     for bulb in Bulb.objects.all():
#         if bulb.enabled == False:
#             continue
#         #print(bulb.name)
#         channel = bulb.channel
#         hue, sat, val = Kasa.scale_hsv(data[channel-1]-1, data[channel], data[channel+1])
#         #print(bulb.ip_addr, channel, hue, sat, val)
#         Kasa.change_color(bulb.ip_addr, hue, sat, val)
#     #stop_time = time.time()
#     #print("--- %s seconds ---" % (stop_time - start_time))
#     rx_something.last_update = time.time()
# rx_something.last_update = None

from lib.dmx_python_client.dmx_client import DmxClient
from lib.dmx_python_client.dmx_client import DmxClientCallback

class MyDmxCallback(DmxClientCallback):
    """
    Example implementation of all available callback methods
    """
    def __init__(self):
        self.last_update = time.time()

    def sync_lost(self) -> None:
        print("SYNC LOST")

    def sync_found(self) -> None:
        print("SYNC FOUND")

    def data_received(self, monitored_data: dict[int, int]) -> None:
        #print("VALID MONITORED DATA: %s" % monitored_data)
        pass

    def full_data_received(self, data: bytes) -> None:
        if time.time()-self.last_update < .01:
            return
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
        self.last_update = time.time()

def do_something():
    # a = StupidArtnetServer()
    # a.register_listener(universe=0, callback_function=rx_something)
    c = DmxClient('/dev/ttyAMA0', [1], MyDmxCallback())
    c.run()
    # print("after-run")
    # while True:
    #     time.sleep(100)




def start_scheduler():
    scheduler = Scheduler()
    scheduler.every().second.do(do_something)
    scheduler.run_continuously()