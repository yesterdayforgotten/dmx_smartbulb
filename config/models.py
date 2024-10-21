from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator

class Bulb(models.Model):
    name    = models.CharField(max_length=100)
    ip_addr = models.GenericIPAddressField(protocol="IPv4", default="0.0.0.0")
    channel = models.IntegerField(default=0,validators=[MaxValueValidator(511), MinValueValidator(1)])
    enabled = models.BooleanField(default=0)