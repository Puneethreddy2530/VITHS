import urllib.request
import time

try:
    print('Connecting...')
    req = urllib.request.urlopen('http://127.0.0.1:8888/video_feed', timeout=5)
    print('Connected. Reading 500 bytes...')
    data = req.read(500)
    print('Data received: ', len(data))
    print(data[:50])
except Exception as e:
    print('Error:', e)
