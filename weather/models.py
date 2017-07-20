# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
from decimal import Decimal
from datetime import timedelta, time, datetime
from django.conf import settings
from django.contrib.gis.db import models
from django.db.models import Max, Min
from django.utils import timezone
from django.utils.encoding import force_text, python_2_unicode_compatible
import logging
import math
import pytz
import re
import telnetlib

from weather.utils import dew_point, actual_pressure, actual_rainfall


logger = logging.getLogger('weather')
KNOTS_TO_MS = Decimal('0.51444')
KNOTS_TO_KS = Decimal('1.85166')
TIMEZONE = pytz.timezone(settings.TIME_ZONE)


@python_2_unicode_compatible
class Location(models.Model):
    """Represents the location of a weather station.
    """
    title = models.CharField(blank=True, max_length=128)
    description = models.TextField(blank=True)
    point = models.PointField()
    height = models.DecimalField(max_digits=7, decimal_places=3)

    def __str__(self):
        return force_text('{} {}'.format(self.title, self.point.tuple))


@python_2_unicode_compatible
class WeatherStation(models.Model):
    """Represents an automatic weather station (AWS) installation.
    """
    MANUFACTURER_CHOICES = (
        ('telvent', 'Telvent'),
        ('vaisala', 'Vaisala'),
    )
    name = models.CharField(max_length=100)
    location = models.ForeignKey(Location, null=True, blank=True)
    abbreviation = models.CharField(max_length=20, unique=True, help_text='Internal abbreviation code')
    bom_abbreviation = models.CharField(
        max_length=4, unique=True, verbose_name='BoM abbreviation',
        help_text='Bureau of Meteorology site abbrevation code')
    ip_address = models.GenericIPAddressField(verbose_name='IPv4 address')
    port = models.PositiveIntegerField(default=43000)
    last_scheduled = models.DateTimeField()
    last_reading = models.DateTimeField()
    battery_voltage = models.DecimalField(max_digits=3, decimal_places=1)
    connect_every = models.PositiveSmallIntegerField(default=15)
    active = models.BooleanField(default=False)
    manufacturer = models.CharField(
        max_length=100, null=True, blank=True, choices=MANUFACTURER_CHOICES)
    upload_data = models.BooleanField(
        default=True, help_text='Upload observation data to external consumers (e.g. DAFWA)')

    class Meta:
        ordering = ['-last_reading']

    def last_reading_local(self):
        """Return the datetime of the latest reading, offset to local time.
        """
        return self.last_reading.astimezone(TIMEZONE)

    def last_reading_time(self):
        """Reading local time of latest reading in 24h time (HHMM).
        """
        return datetime.strftime(self.last_reading_local(), '%H%M')

    def rain_since_nine_am(self):
        now = timezone.make_aware(datetime.now(), TIMEZONE)

        if now.time() > time(9):
            last_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            yesterday = now - timedelta(hours=24)
            last_9am = yesterday.replace(hour=9, minute=0, second=0, microsecond=0)
        try:
            rain_stats = self.readings.filter(rainfall__gt=0, date__gte=last_9am)
            rain_stats = rain_stats.aggregate(Min('rainfall'), Max('rainfall'))
            return rain_stats['rainfall__max'] - rain_stats['rainfall__min']
        except:
            return 0

    def download_observation(self):
        """Utility function to connect to a station via Telnet and download a
        single observation.
        """
        retrieval_time = timezone.now().replace(microsecond=0)
        logger.info('Trying to connect to {}'.format(self.name))
        client, output = None, False
        try:
            client = telnetlib.Telnet(self.ip_address, self.port)
            if self.manufacturer == 'vaisala':
                pattern = '(^0[Rr]0),([A-Za-z]{2}=-?(\d+(\.\d+)?)[A-Za-z#],?)+'
                clean_response = False
                while not clean_response:
                    response = client.read_until('\r\n'.encode('utf8'), 60)
                    m = re.search(pattern, response)
                    if m:
                        response = response.strip()
                        clean_response = True
            else:  # Default to using the Telvent station format.
                response = client.read_until('\r\n'.encode('utf8'), 60)
                response = response[2:]
            if client:
                client.close()
        except Exception as e:
            logger.error('Failed to read weather data from {}'.format(self.name))
            logger.exception(e)
            return None

        logger.info('PERIODIC READING OF {}'.format(self.name))
        logger.info(force_text(response))
        output = (self.pk, force_text(response), retrieval_time)
        return output

    def save_observation(self, raw_data, timestamp=None):
        """Convert a raw observation and save it to the database.
        """
        if timestamp is None:
            timestamp = timezone.now()

        # Short-circuit: prevent this method being called multiple times in
        # close succession and creating duplicates.
        o = WeatherObservation.objects.filter(station=self).first()
        if o and (timestamp - o.date).seconds < 30:  # Arbitrary time limit.
            return None

        empty = Decimal('0.00')
        observation = WeatherObservation()

        # Create a weather observation from the retrieved data.
        if self.manufacturer == 'vaisala':
            # Vaisala data is a comma-separated NVP format.
            items = raw_data.split(',')
            items.pop(0)  # Remove the first element of the raw data.
            data = {}
            for item in items:
                k, v = item.split('=')
                data[k] = v
            pattern = '(\d+(\.\d+)?)(.+)'
            observation.temperature = Decimal(re.search(pattern, data['Ta']).group(1))
            observation.humidity = Decimal(re.search(pattern, data['Ua']).group(1))
            observation.dew_point = dew_point(
                float(observation.temperature), float(observation.humidity))
            observation.pressure = Decimal(re.search(pattern, data['Pa']).group(1))
            observation.wind_direction_min = Decimal(re.search(pattern, data['Dn']).group(1))
            observation.wind_direction_max = Decimal(re.search(pattern, data['Dx']).group(1))
            observation.wind_direction = Decimal(re.search(pattern, data['Dm']).group(1))
            observation.wind_speed_min = Decimal(re.search(pattern, data['Sn']).group(1)) * KNOTS_TO_MS
            observation.wind_speed_min_kn = Decimal(re.search(pattern, data['Sn']).group(1))
            observation.wind_speed_max = Decimal(re.search(pattern, data['Sx']).group(1)) * KNOTS_TO_MS
            observation.wind_speed_max_kn = Decimal(re.search(pattern, data['Sx']).group(1))
            observation.wind_speed = Decimal(re.search(pattern, data['Sm']).group(1)) * KNOTS_TO_MS
            observation.wind_speed_kn = Decimal(re.search(pattern, data['Sm']).group(1))
            observation.rainfall = Decimal(re.search(pattern, data['Rc']).group(1))
            observation.actual_rainfall = actual_rainfall(
                Decimal(observation.rainfall), self, timestamp)
            observation.actual_pressure = actual_pressure(
                float(observation.temperature), float(observation.pressure),
                float(self.location.height))
            observation.raw_data = raw_data
            observation.station = self
            observation.save()
            self.last_reading = timestamp
            self.battery_voltage = Decimal(re.search(pattern, data['Vs']).group(1))
            self.save()
        else:  # Default to using the Telvent station format.
            # Telvent data is stored in NVP format separated by the pipe symbol '|'.
            #   |<NAME>=<VALUE>|
            items = raw_data.split('|')
            data = {}
            for item in items:
                if (item != ''):
                    try:
                        k, v = item.split('=')
                        data[k] = v
                    except:
                        pass
            observation.temperature_min = data.get('TN') or empty
            observation.temperature_max = data.get('TX') or empty
            observation.temperature = data.get('T') or empty
            observation.temperature_deviation = data.get('TS') or empty
            observation.temperature_outliers = data.get('TO') or 0
            observation.pressure_min = data.get('QFEN') or empty
            observation.pressure_max = data.get('QFEX') or empty
            observation.pressure = data.get('QFE') or empty
            observation.pressure_deviation = data.get('QFES') or empty
            observation.pressure_outliers = data.get('QFEO') or 0
            observation.humidity_min = data.get('HN') or empty
            observation.humidity_max = data.get('HX') or empty
            observation.humidity = data.get('H') or empty
            observation.humidity_deviation = data.get('HS') or empty
            observation.humidity_outliers = data.get('HO') or 0
            observation.wind_direction_min = data.get('DN') or empty
            observation.wind_direction_max = data.get('DX') or empty
            observation.wind_direction = data.get('D') or empty
            # Temporary change: reverse the wind_direction for the Mitchell
            # Plateau AWS only, until such time as it is rewired.
            if self.bom_abbreviation == 'MIPL':
                  adjust = float(observation.wind_direction) + 180
                  if adjust >= 360:
                      adjust -= 360
                  observation.wind_direction = Decimal(adjust)
            observation.wind_direction_deviation = data.get('DS') or empty
            observation.wind_direction_outliers = data.get('DO') or 0
            if (data.get('SN')):
                observation.wind_speed_min = Decimal(data.get('SN')) * KNOTS_TO_MS or 0
                observation.wind_speed_min_kn = Decimal(data.get('SN')) or 0
                observation.wind_speed_deviation = Decimal(data.get('SS')) * KNOTS_TO_MS or 0
                observation.wind_speed_outliers = data.get('SO') or 0
                observation.wind_speed_deviation_kn = Decimal(data.get('SS')) or 0
            if (data.get('SX')):
                observation.wind_speed_max = Decimal(data.get('SX')) * KNOTS_TO_MS or 0
                observation.wind_speed_max_kn = Decimal(data.get('SX')) or 0
            if (data.get('S')):
                observation.wind_speed = Decimal(data.get('S')) * KNOTS_TO_MS or 0
                observation.wind_speed_kn = Decimal(data.get('S')) or 0
            observation.rainfall = data.get('R') or empty
            observation.dew_point = dew_point(
                float(observation.temperature), float(observation.humidity))
            observation.actual_rainfall = actual_rainfall(
                Decimal(observation.rainfall), self, timestamp)
            observation.actual_pressure = actual_pressure(
                float(observation.temperature), float(observation.pressure),
                float(self.location.height))
            observation.raw_data = raw_data
            observation.station = self
            observation.save()
            self.last_reading = timestamp
            self.battery_voltage = data.get('BV', empty) or empty
            self.save()

        return observation

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class WeatherObservation(models.Model):
    """Represents an observation of weather from an AWS.

    Capable of storing information from a NVP messsage about the following
    (one minute unless otherwise specified):
        Air temperature in degrees Celsius
            - instantaneous (TI), average (T), minimum (TN), maximum (TX),
              standard deviation (TS), number of outliers (TO), quality (TQ).
        Wet bulb temperature in degrees Celsius
            - instantaneous (WI), average (W), minimum (WN), maximum (WX),
              standard deviation (WS), number of outliers (WS), quality (WQ).
        Station-level atmospheric pressure in hectopascals (not sea-level
        adjusted)
            - instantaneous (QFEI), average (QFE), minimum (QFEN),
              maximum (QFEX), standard deviation (QFES),
              number of outliers (QFEO), quality (QFEQ)
        Relative humidity in percent
            - instantaneous (HI), average (H), minimum (HN), maximum (HX),
              standard deviation (HS), number of outliers (HO), quality (HQ)
        Wind direction in degrees from North
            - instantaneous (DI), average (D), minimum (DN), maximum (DX),
              standard deviation (DS), number of outliers (DO), quality (DQ)
        Wind speed in kilometres per hour
            - instantaneous (SI), average (S), minimum (SN), maximum (SX),
              standard deviation (SS), number of outliers (SO), quality (SQ)
        Rainfall in millimetres
            - total (R)
    """
    station = models.ForeignKey(WeatherStation, related_name='readings')
    date = models.DateTimeField(default=timezone.now)
    raw_data = models.TextField()

    temperature_min = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    temperature_max = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    temperature = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True,
        help_text='Temperature (°Celcius)')
    temperature_deviation = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    temperature_outliers = models.PositiveIntegerField(
        blank=True, null=True)

    pressure_min = models.DecimalField(
        max_digits=5, decimal_places=1, blank=True, null=True)
    pressure_max = models.DecimalField(
        max_digits=5, decimal_places=1, blank=True, null=True)
    pressure = models.DecimalField(
        max_digits=5, decimal_places=1, blank=True, null=True,
        help_text='Pressure (hPa)')
    pressure_deviation = models.DecimalField(
        max_digits=5, decimal_places=1, blank=True, null=True)
    pressure_outliers = models.PositiveIntegerField(
        blank=True, null=True)

    humidity_min = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    humidity_max = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    humidity = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True,
        help_text='Relative humidity (%)')
    humidity_deviation = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    humidity_outliers = models.PositiveIntegerField(
        blank=True, null=True)

    wind_direction_max = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_direction_min = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_direction = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True,
        help_text='Wind direction (° from N)')
    wind_direction_deviation = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_direction_outliers = models.PositiveIntegerField(
        blank=True, null=True)

    wind_speed_max = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True,
        help_text='Wind gust (km/h)')
    wind_speed_min = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_speed = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True,
        help_text='Wind speed (km/h)')
    wind_speed_deviation = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_speed_outliers = models.PositiveIntegerField(
        blank=True, null=True)

    wind_speed_max_kn = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_speed_min_kn = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_speed_kn = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    wind_speed_deviation_kn = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    # rainfall represents the rain counter value for the station at the time
    # of observation (which may periodically be reset).
    rainfall = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    # actual_rainfall represents the calculated rainfall (mm) over the
    # previous one minute (normalised where observations occur less often
    # than once/minute).
    actual_rainfall = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)
    # actual_pressure represents the calculated sea-level adjusted atmospheric
    # pressure for the observation (based on the station altitude).
    actual_pressure = models.DecimalField(
        max_digits=5, decimal_places=1, blank=True, null=True)
    dew_point = models.DecimalField(
        max_digits=4, decimal_places=1, blank=True, null=True)

    def __str__(self):
        return '{} at {}'.format(self.station.name, self.date)

    class Meta:
        ordering = ['-date']
        unique_together = ('station', 'date')

    def gm_date(self):
        import calendar
        return calendar.timegm(self.date.timetuple())*1000

    def local_date(self):
        return timezone.localtime(self.date).isoformat().rsplit('.')[0]

    def dew_point(self):
        """
        Given the relative humidity and the dry bulb (actual) temperature,
        calculates the dew point (one-minute average).

        The constants a and b are dimensionless, c and d are in degrees
        celsius.

        Using the equation from:
             Buck, A. L. (1981), "New equations for computing vapor pressure
             and enhancement factor", J. Appl. Meteorol. 20: 1527-1532
        """
        T = float(self.temperature)
        RH = float(self.humidity)

        if not RH:
            return '0.0'

        d = 234.5

        if T > 0:
            # Use the set of constants for 0 <= T <= 50 for <= 0.05% accuracy.
            b = 17.368
            c = 238.88
        else:
            # Use the set of constants for -40 <= T <= 0 for <= 0.06% accuracy.
            b = 17.966
            c = 247.15

        gamma = math.log(RH / 100 * math.exp((b - (T / d)) * (T / (c + T))))
        return '%.2f' % ((c * gamma) / (b - gamma))
    dew_point.short_description = 'Dew point (°Celsius)'

    def get_pressure(self):
        """
        Convert the pressure from absolute pressure into sea-level adjusted
        atmospheric pressure.
        Uses the barometric formula.
        Returns the mean sea-level pressure values in hPa.
        """
        temp = float(self.temperature) + 273.15
        pressure = float(self.pressure) * 100
        g0 = 9.80665
        M = 0.0289644
        R = 8.31432
        lapse_rate = -0.0065
        height = float(getattr(self.station.location, 'height', 0))
        return '%0.2f' % (pressure / math.pow(
            temp / (temp + (lapse_rate * height)),
            (g0 * M) / (R * lapse_rate)) / 100)

    def get_dafwa_obs(self):
        """Return a list of observation information that is compatible with
        being transmitted to DAFWA (typically as a CSV).
        All list elements should be strings.
        """
        reading_date = timezone.localtime(self.date)
        return [
            unicode(self.station.bom_abbreviation),
            unicode(reading_date.strftime('%Y-%m-%d')),
            unicode(reading_date.strftime('%H:%M:%S')),
            '{:.1f}'.format(float(self.temperature)),
            '{:.1f}'.format(float(self.humidity)),
            '{:.1f}'.format(float(self.wind_speed)),
            '{:.1f}'.format(float(self.wind_speed_max)),
            '{:.1f}'.format(float(self.wind_direction)),
            '{:.1f}'.format(float(self.actual_rainfall)),
            '{:.1f}'.format(float(self.station.battery_voltage)),
            '',  # Solar power (watts/m2) - not calculated
            '{:.1f}'.format(float(self.actual_pressure))
        ]
