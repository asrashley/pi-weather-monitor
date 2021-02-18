import csv
import datetime
from pathlib import Path
import random
import threading
import time
from typing import List, NamedTuple, Optional

from PIL import Image, ImageDraw, ImageFont

DEVMODE = False

try:
    from luma.core.interface.serial import spi
    from luma.oled.device import ssd1351
    import RPi.GPIO as GPIO
    from weather.bme280 import Bme280Probe
except ImportError as err:
    print(f'Using development mode: {err}')
    import cv2
    import numpy as np
    DEVMODE = True

class WeatherProbe(NamedTuple):
    name: str
    colour: str
    units: str

class Sample(NamedTuple):
    timestamp: float # datetime.datetime
    values: List[int]

class ModeFont(NamedTuple):
    heading: ImageFont.ImageFont
    body: ImageFont.ImageFont
    scale: Optional[ImageFont.ImageFont]

class DevDisplay:
    """
    Mock version display driver that uses CV
    """
    def __init__(self):
        self.active = False

    def display(self, image) -> None:
        npImage = np.asarray(image)
        frameBGR = cv2.cvtColor(npImage, cv2.COLOR_RGB2BGR)
        cv2.imshow('Weather', frameBGR)

    def clear(self):
        pass

    def show(self):
        self.active = True

    def hide(self):
        self.active = False

class WeatherMonitor:
    degree_sign= u'\N{DEGREE SIGN}'

    TEMP_SENSOR_TEMPLATE = r'/sys/bus/w1/devices/{id}/temperature'
    CSV_FILENAME_TEMPLATE = r'weather-{year:04d}{month:02d}{day:02d}.csv'
    CSV_TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S.%f'

    INSIDE_ID = '28-012042099590'
    OUTSIDE_ID = '28-01204230055e'
    SOIL_ID = '28-01204239435a'

    DISPLAY_TIMEOUT = 60 * 2
    PAGE_TIMEOUT = 5

    PROBES = [
        WeatherProbe('humidity', '#3333ff', '%'),
        WeatherProbe('pressure', '#777777', 'hPa'),
        WeatherProbe('inside', 'yellow', degree_sign + 'C'),
        WeatherProbe('outside', 'green', degree_sign + 'C'),
        WeatherProbe('soil', '#553300', degree_sign + 'C')
    ]

    SAMPLES_PER_HOUR = 60

    RED_BUTTON_GPIO = 5
    BLUE_BUTTON_GPIO = 6
    GREEN_BUTTON_GPIO = 16
    YELLOW_BUTTON_GPIO = 26

    def __init__(self, dev: bool, width: int = 128, height: int = 128):
        self.devmode = dev
        self.width = width
        self.height = height
        self.image = Image.new('RGB', (width, height), 'white')
        self.page = len(self.PROBES)
        self.finished = False
        self.fonts = [
            ModeFont(ImageFont.truetype("FreeSansBold.ttf", 12),
                     ImageFont.truetype("FreeSansBold.ttf", 20),
                     None),
            ModeFont(ImageFont.truetype("FreeSansBold.ttf", 18),
                     ImageFont.truetype("FreeSansBold.ttf", 26),
                     ImageFont.truetype("FreeSansBold.ttf", 14)),
            ModeFont(ImageFont.truetype("FreeSansBold.ttf", 10),
                     None,
                     None),
        ]
        self.samples: List[Sample] = []
        self.next_display_timeout = time.time() + self.DISPLAY_TIMEOUT
        self.next_page_timeout = time.time() + self.PAGE_TIMEOUT
        self.hidden = False
        self.display_off = False
        self.probe_thread = threading.Thread(target=self.read_probes, daemon=True)
        self.cond = threading.Condition()
        if dev:
            self.device = DevDisplay()
        else:
            serial = spi(device=0, port=0)
            self.device = ssd1351(serial, rotate=2, bgr=True)
            self.device.clear()
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.RED_BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(self.RED_BUTTON_GPIO, GPIO.FALLING,
                                  callback=self.on_red_button, bouncetime=250)
            GPIO.setup(self.BLUE_BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(self.BLUE_BUTTON_GPIO, GPIO.FALLING,
                                  callback=self.on_blue_button, bouncetime=250)
            GPIO.setup(self.GREEN_BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(self.GREEN_BUTTON_GPIO, GPIO.FALLING,
                                  callback=self.on_green_button, bouncetime=250)
            GPIO.setup(self.YELLOW_BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(self.YELLOW_BUTTON_GPIO, GPIO.FALLING,
                                  callback=self.on_yellow_button, bouncetime=250)

    def show(self) -> None:
        """
        draw the current page
        """
        draw = ImageDraw.Draw(self.image)
        draw.rectangle([(0,0), (self.width, self.height)], 'black', 'black')
        now = datetime.datetime.now()
        timestr = now.strftime('%H:%M:%S')
        width, height = self.fonts[2].heading.getsize(timestr)
        draw.text(((0, 0)), timestr, fill='white', font=self.fonts[2].heading)
        timestr = now.strftime('%a %d/%m/%Y')
        width, height2 = self.fonts[2].heading.getsize(timestr)
        draw.text(((self.width - width, 0)), timestr, fill='white', font=self.fonts[2].heading)
        height = max(height, height2)
        if self.samples:
            last_sample = self.samples[-1]
        else:
            last_sample = Sample(datetime.datetime.now().timestamp(),
                                 [None] * len(self.PROBES))
        if self.page >= len(self.PROBES):
            self.show_all(draw, last_sample, height + 1)
        else:
            self.show_single(draw, last_sample, height + 1)
        self.device.display(self.image)

    def show_all(self, draw: ImageDraw.Draw, last_sample: Sample,
                 top_line: int) -> None:
        gap = (self.height - top_line) // len(self.PROBES)
        ypos = top_line
        for index, value in enumerate(last_sample.values):
            probe = self.PROBES[index]
            draw.text(((0, ypos)), probe.name, fill=self.PROBES[index].colour,
                      font=self.fonts[0].heading)
            valstr = self.value_str(probe, value)
            size = draw.textlength(valstr, font=self.fonts[0].body)
            xpos = self.width - size
            draw.text(((xpos, ypos)), valstr, fill=self.PROBES[index].colour,
                      font=self.fonts[0].body)
            ypos += gap

    def show_single(self, draw: ImageDraw.Draw, last_sample: Sample,
                    top_line: int) -> None:
        probe = self.PROBES[self.page]
        width, ypos = self.fonts[1].heading.getsize(probe.name)
        xpos = (self.width - width) // 2
        draw.text(((xpos, top_line)), probe.name, fill=probe.colour,
                  font=self.fonts[1].heading)
        value = self.value_str(probe, last_sample.values[self.page])
        width, height = self.fonts[1].body.getsize(value)
        xpos = self.width - width
        ypos += 2 + top_line
        draw.text(((xpos, ypos)), value, fill=probe.colour,
                  font=self.fonts[1].body)
        ypos += height + 2
        num_samples = len(self.samples)
        if num_samples < 3:
            return
        if num_samples < self.width:
            points = [s.values[self.page] for s in self.samples]
        else:
            scale = num_samples // self.width
            points: List[float] = []
            for i in range(0, num_samples, scale):
                pts = [s.values[self.page] for s in self.samples[i:i+scale]]
                points.append(float(sum(pts)) / len(pts))
        step = max(1, self.width / float(len(points)))
        min_val = min(points) * 0.9
        max_val = max(points) * 1.1
        draw.text((0, ypos), '{0:3.0F}'.format(max_val), fill='yellow')
        draw.text((0, self.height - 12), '{0:3.0F}'.format(min_val), fill='cyan')
        xpos = 0
        yscale = (self.height - ypos) / float(max(1, max_val - min_val))
        coords = []
        for pt in points:
            ypos = self.height - (pt - min_val) * yscale
            coords.append((xpos, ypos))
            xpos += step
        draw.line(coords, fill='white')
        if min_val < 0 and max_val >= 0:
            ypos = self.height + min_val * yscale
            draw.line(((0, ypos), (self.width, ypos)), fill='#777')

    def value_str(self, probe: WeatherProbe, value: float) -> str:
        if value is None:
            return '--.-'
        if value >= 100.0:
            return '{0:>4d}{1}'.format(round(value), probe.units)
        return '{0:>4.1F}{1}'.format(value, probe.units)

    def read_w1_sensor(self, id: str) -> float:
        """read temperature from one wire probe"""
        try:
            with open(self.TEMP_SENSOR_TEMPLATE.format(id=id), 'rt') as src:
                value = src.read()
                return int(value, 10) / 1000.0
        except FileNotFoundError as err:
            print(err)
            return 0

    def read_probes(self):
        """thread that reads the data from the various probes"""
        multi_probe = Bme280Probe()
        # limit in-memory samples to the last 24 hours
        max_samples = 24 * 60 * 60 // self.SAMPLES_PER_HOUR
        while not self.finished:
            next_sample = time.time() + (3600.0 / self.SAMPLES_PER_HOUR)
            temperature, pressure, humidity = multi_probe.read_values()
            inside = self.read_w1_sensor(self.INSIDE_ID)
            outside = self.read_w1_sensor(self.OUTSIDE_ID)
            soil = self.read_w1_sensor(self.SOIL_ID)
            inside = (temperature + inside) / 2.0
            values = [humidity, pressure, inside, outside, soil]
            sample = Sample(datetime.datetime.now().timestamp(), values)
            with self.cond:
                self.samples.append(sample)
                self.samples = self.samples[-max_samples:]
            self.append_sample_to_csv(sample)
            delay = next_sample - time.time()
            if delay > 0:
                time.sleep(delay)

    def unblank_display(self):
        self.next_display_timeout = time.time() + self.DISPLAY_TIMEOUT
        self.next_page_timeout = time.time() + self.PAGE_TIMEOUT
        self.hidden = False

    def on_red_button(self, param):
        """called when red button is pressed"""
        # print('red', type(param), param, self.page)
        with self.cond:
            self.unblank_display()
            self.page = (1 + self.page) % (1 + len(self.PROBES))
            self.cond.notify_all()

    def on_green_button(self, param):
        """called when green button is pressed"""
        # print('green', type(param), param, self.page)
        with self.cond:
            self.page -= 1
            if self.page < 0:
                self.page = len(self.PROBES)
            self.unblank_display()
            self.cond.notify_all()

    def on_blue_button(self, param):
        """called when blue button is pressed"""
        # print('blue', type(param), param)
        with self.cond:
            self.unblank_display()
            self.cond.notify_all()

    def on_yellow_button(self, param):
        """called when yellow button is pressed"""
        # print('yellow', type(param), param)
        with self.cond:
            self.unblank_display()
            self.cond.notify_all()

    def read_csv_file(self) -> None:
        today = datetime.date.today()
        filename = Path(self.CSV_FILENAME_TEMPLATE.format(
            year=today.year, month=today.month, day=today.day))
        if not filename.exists():
            return
        self.samples = []
        with filename.open('rt') as src:
            reader = csv.DictReader(src)
            for row in reader:
                try:
                    values: List[float] = [float(row.get(probe.name, 0)) for probe in self.PROBES]
                    timestamp = datetime.datetime.strptime(
                        row['timestamp'], self.CSV_TIMESTAMP_FORMAT)
                    sample = Sample(timestamp.timestamp(), values)
                    self.samples.append(sample)
                except (KeyError, TypeError) as err:
                    print(err)
                    print([probe.name for probe in self.PROBES])
                    print(row)
                    print('=====')

    def append_sample_to_csv(self, sample: Sample) -> None:
        timestamp = datetime.datetime.fromtimestamp(sample.timestamp)
        filename = Path(self.CSV_FILENAME_TEMPLATE.format(
            year=timestamp.year, month=timestamp.month,
            day=timestamp.day))
        new_file = not filename.exists()
        with filename.open('ta') as dst:
            if new_file:
                dst.write('timestamp,')
                dst.write(','.join([p.name for p in self.PROBES]))
                dst.write('\n')
            dst.write(timestamp.strftime(self.CSV_TIMESTAMP_FORMAT)+',')
            dst.write(','.join([str(v) for v in sample.values]))
            dst.write('\n')

    def run(self):
        self.read_csv_file()
        self.probe_thread.start()
        self.show()
        if self.devmode:
            k = cv2.waitKey(1) & 0xFF
        time.sleep(1)
        self.next_display_timeout = time.time() + self.DISPLAY_TIMEOUT
        self.next_page_timeout = time.time() + self.PAGE_TIMEOUT
        while not self.finished:
            if not self.hidden:
                if self.display_off:
                    self.device.show()
                    self.display_off = False
                with self.cond:
                    self.show()
                if time.time() > self.next_display_timeout:
                    self.hidden = True
                    self.device.hide()
                    self.display_off = True
            with self.cond:
                if not self.cond.wait(timeout=1):
                    if time.time() > self.next_page_timeout:
                        self.page = (self.page + 1) % (1 + len(self.PROBES))
                        self.next_page_timeout = time.time() + self.PAGE_TIMEOUT
            if self.devmode:
                k = cv2.waitKey(1) & 0xFF
                if k == 27:
                    self.finished = True
        if self.devmode:
            cv2.destroyAllWindows()
        self.probe_thread.join()

if __name__ == "__main__":
    wm = WeatherMonitor(DEVMODE)
    wm.run()
