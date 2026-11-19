from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FuelStation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("opis_id", models.IntegerField(unique=True)),
                ("name", models.CharField(max_length=200)),
                ("address", models.CharField(max_length=200)),
                ("city", models.CharField(max_length=100)),
                ("state", models.CharField(max_length=2)),
                ("rack_id", models.IntegerField(blank=True, null=True)),
                ("price", models.FloatField(help_text="Retail price, USD per gallon")),
                ("lat", models.FloatField()),
                ("lon", models.FloatField()),
            ],
            options={
                "indexes": [models.Index(fields=["state", "city"], name="routeplanne_state_84c790_idx")],
            },
        ),
    ]
