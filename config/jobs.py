from django.db.models import F
from multiprocessing import Process, Lock, Array
import time
import signal
from .kasabulb.kasabulb import Kasa
from .models import Bulb
from lib.dmx_python_client.dmx_client import DmxClient
from lib.dmx_python_client.dmx_client import DmxClientCallback
from django import db

class MyDmxCallback(DmxClientCallback):
    def __init__(self, data_array, data_lock):
        self.data_array = data_array
        self.data_lock = data_lock

    def sync_lost(self) -> None:
        print("DmxClient: SYNC LOST", flush=True)

    def sync_found(self) -> None:
        print("DnxClient: SYNC FOUND", flush=True)

    def data_received(self, monitored_data: dict[int, int]) -> None:
        pass

    def full_data_received(self, data: bytes) -> None:
        self.data_lock.acquire()
        for i in range(len(self.data_array)):
            self.data_array[i] = data[i]
        self.data_lock.release()

class GracefulExit(Exception):
    pass

def signal_handler(signum, frame):
    raise GracefulExit

def dmx_receiver(data_array, data_lock):
    try: 
        print("dmx_receiver: Starting", flush=True)
        # a = StupidArtnetServer()
        # a.register_listener(universe=0, callback_function=rx_something)
        while True:
            try:
                print("DmxClient: Starting", flush=True);
                c = DmxClient('/dev/ttyAMA3', [1], MyDmxCallback(data_array, data_lock))
                c.run()
            except GracefulExit:
                raise
            except Exception as e:
                print("DMX Exception:", e)
                time.sleep(2)
    except GracefulExit:
        print("dmx_receiver exiting gracefully")

def bulb_updater(data_array, data_lock):
    try: 
        print("bulb_updater: Starting", flush=True)
        dmx_data = bytearray()
        last_update = time.time()
        while True:
            #if time.time()-self.last_update < .01:
            #    return
            data_lock.acquire()
            dmx_data = bytearray(data_array)
            data_lock.release()
            #start_time = time.time()
            for bulb in Bulb.objects.all():
                if bulb.enabled == False:
                    continue
                # print(bulb.name, flush=True)
                channel = bulb.channel
                hue, sat, val = Kasa.scale_hsv(dmx_data[channel-1]-1, dmx_data[channel], dmx_data[channel+1])
                #print(bulb.ip_addr, channel, hue, sat, val, flush=True)
                Kasa.change_color(bulb.ip_addr, hue, sat, val)
            #stop_time = time.time()
            #print("--- %s seconds ---" % (stop_time - start_time))
            #last_update = time.time()
            time.sleep(.1)
    except GracefulExit:
        print("bulb_updater exiting gracefully")

def start_background_processes():
    data_array = Array('B', [0]*512)
    data_lock  = Lock()

    db.connections.close_all()

    signal.signal(signal.SIGTERM, signal_handler)
    dmx_process  = Process(target=dmx_receiver, args=(data_array, data_lock,))
    bulb_process = Process(target=bulb_updater, args=(data_array, data_lock,))

    # while True:
    #     data_lock.acquire()
    #     data_lock.release()

    dmx_process.daemon = True
    bulb_process.daemon = True

    dmx_process.start()
    bulb_process.start()
