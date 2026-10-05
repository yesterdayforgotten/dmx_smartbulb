from django.db.models import F
from multiprocessing import Process, Lock, Array, Value
import time
import signal
import serial
from .kasabulb.kasabulb import Kasa
from .models import Bulb
# from lib.dmx_python_client.dmx_client import DmxClient
# from lib.dmx_python_client.dmx_client import DmxClientCallback
from django import db

# class MyDmxCallback(DmxClientCallback):
#     def __init__(self, data_array, data_lock):
#         self.data_array = data_array
#         self.data_lock = data_lock
#         self.sync = False

#     def sync_lost(self) -> None:
#         if (self.sync == True):
#             print("DmxClient: SYNC LOST", flush=True)
#         self.sync = False

#     def sync_found(self) -> None:
#         if (self.sync == False):
#             print("DnxClient: SYNC FOUND", flush=True)
#         self.sync = True

#     def data_received(self, monitored_data: dict[int, int]) -> None:
#         pass

#     def full_data_received(self, data: bytes) -> None:
#         self.data_lock.acquire()
#         for i in range(len(self.data_array)):
#             self.data_array[i] = data[i]
#         self.data_lock.release()

# def dmx_receiver(data_array, data_lock):
#     try: 
#         print("dmx_receiver: Starting", flush=True)
#         # a = StupidArtnetServer()
#         # a.register_listener(universe=0, callback_function=rx_something)
#         while True:
#             try:
#                 print("DmxClient: Starting", flush=True);
#                 c = DmxClient('/dev/ttyAMA3', [1], MyDmxCallback(data_array, data_lock))
#                 c.run()
#             except GracefulExit:
#                 raise
#             except Exception as e:
#                 print("DMX Exception:", e)
#                 time.sleep(2)
#     except GracefulExit:
#         print("dmx_receiver exiting gracefully")

class GracefulExit(Exception):
    pass

def signal_handler(signum, frame):
    raise GracefulExit


DMX_DATA_LEN = 512
DMX_SERIAL_HEADER_LEN = 4
DMX_SERIAL_LEN = DMX_DATA_LEN + DMX_SERIAL_HEADER_LEN

def serial_receiver(data_array, data_lock, data_valid):
    ser = serial.Serial(port="/dev/ttyAMA3", baudrate=921600)

    while True:
        line = ser.read(size=DMX_SERIAL_LEN)
        start_offset = line.find(b'DMX#')

        while start_offset == -1:
            print("Couldn't find DMX# header, trying again")
            line += ser.read(size=DMX_SERIAL_HEADER_LEN)
            start_offset = line.find(b'DMX#')

        if start_offset != 0:
            print("tweaking, start_offset: " + str(start_offset))
            line = line[start_offset:] + ser.read(size=start_offset)
        
        # print("length: " +str(len(line)-4))
        data_lock.acquire()
        for i in range(min(len(line)-DMX_SERIAL_HEADER_LEN, len(data_array))):
            data_array[i] = line[i+DMX_SERIAL_HEADER_LEN]
        data_valid.value = True
        data_lock.release()
        # print(bytearray(data_array).hex(), flush=True)


prev_values = [0,0,0] * 512
 
def bulb_updater(data_array, data_lock, data_valid):
    global prev_values
    force = True
    try: 
        print("bulb_updater: Starting", flush=True)
        dmx_data = bytearray()
        last_update = time.time()
        while True:
            if data_valid.value:
                if time.time() - last_update > 2:
                    force = True
                else:
                    force = False

                data_lock.acquire()
                dmx_data = bytearray(data_array)
                data_lock.release()

                #print(dmx_data.hex())
                #start_time = time.time()
                for bulb in Bulb.objects.all():
                    if bulb.enabled == False:
                        continue
                    # print(bulb.name, flush=True)
                    channel = bulb.channel
                    hue, sat, val = Kasa.scale_hsv(dmx_data[channel-1], dmx_data[channel], dmx_data[channel+1])
                    #print ("prev:")
                    #print (prev_values[channel])
                    #print ("curr:")
                    #print ([hue,sat,val])
                    #print (force)
                    if (prev_values[channel] == [hue, sat, val]) and not force:
                        continue
                    prev_values[channel] = [hue, sat, val]
                    #print(bulb.ip_addr, channel, hue, sat, val, flush=True)
                    Kasa.change_color(bulb.ip_addr, hue, sat, val)
                    time.sleep(.005)
                #stop_time = time.time()
                # print("--- %s seconds ---" % (stop_time - start_time))
                if force:
                    print ("force")
                    last_update = time.time()
                #last_update = time.time()
            time.sleep(.1)
    except GracefulExit:
        print("bulb_updater exiting gracefully")

def start_background_processes():
    print("start_background_processes: Starting", flush=True)
    data_array = Array('B', [0]*DMX_DATA_LEN)
    data_lock  = Lock()
    data_valid = Value('b', False)

    db.connections.close_all()

    signal.signal(signal.SIGTERM, signal_handler)
    # dmx_process  = Process(target=dmx_receiver, args=(data_array, data_lock,))
    serial_process = Process(target=serial_receiver, args=(data_array, data_lock, data_valid))
    bulb_process = Process(target=bulb_updater, args=(data_array, data_lock, data_valid))

    # dmx_process.daemon = True
    serial_process.daemon = True
    bulb_process.daemon = True

    # dmx_process.start()
    serial_process.start()
    bulb_process.start()
