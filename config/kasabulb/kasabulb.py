import socket
import json

class Kasa:
    @staticmethod
    def scale_hsv(hue, sat, val):
        h = 180 if (sat == 0) else (hue * 360) // 255
        s = (sat * 100) // 255
        v = (val * 100) // 255
        return h,s,v
    
    @staticmethod
    def encrypt(string) -> bytearray:
        key = 171
        result = bytearray(string.encode())
        for i in range(len(result)):
            key ^= result[i]
            result[i] = key
        return result

    @staticmethod
    def decrypt(bytes) -> str:
        key = 171
        result = bytearray(bytes)
        for i in range(len(result)):
            temp = result[i]
            result[i] ^= key
            key = temp
        return result.decode()

    @staticmethod
    def discovery():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(Kasa.encrypt('{"system":{"get_sysinfo":null}}'), ("255.255.255.255", 9999))
        sock.settimeout(2)

        kasa_devices = {}
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                data = json.loads(Kasa.decrypt(data))
                if data['system']['get_sysinfo']['mic_type'] == "IOT.SMARTBULB":
                    kasa_devices[addr[0]] = data
            except:
               break
        sock.close()

        return kasa_devices

    @staticmethod
    def send_kasa_packet(ip_addr, port, data):
        data = Kasa.encrypt(json.dumps(data))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(data, (ip_addr, port))
        sock.close()

    @staticmethod
    def send_kasa_packet_and_response(ip_addr, port, data):
        data = Kasa.encrypt(json.dumps(data))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(data, (ip_addr, port))
        sock.settimeout(.5)
        try:
            data, addr = sock.recvfrom(4096)
            data = json.loads(Kasa.decrypt(data))
        except:
            data = "timeout"
        sock.close()
        return data

    @staticmethod
    def change_color(ip_addr, hue, sat, val):
        transition_period = 30
        #transition_period = 0
        data = {
            'smartlife.iot.smartbulb.lightingservice': {
                'transition_light_state': {
                    'hue': hue,
                    'saturation': sat,
                    'brightness': val,
                    'on_off': 0 if val == 0 else 1,
                    'color_temp': 0,
                    'ignore_default': 1,
                    'transition_period': transition_period
                }
            }
        }
        Kasa.send_kasa_packet(ip_addr, 9999, data)

    @staticmethod
    def get_state(ip_addr):
        data = {'system': {'get_sysinfo': 'null'}}
        return Kasa.send_kasa_packet_and_response(ip_addr, 9999, data)
        