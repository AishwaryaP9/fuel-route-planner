from django.db import models


class FuelStation(models.Model):
    """A truck stop from the fuel price file, placed at its city centre."""

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField(null=True, blank=True)
    price = models.FloatField(help_text="Retail price, USD per gallon")
    lat = models.FloatField()
    lon = models.FloatField()

    class Meta:
        indexes = [models.Index(fields=["state", "city"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price:.3f}"
