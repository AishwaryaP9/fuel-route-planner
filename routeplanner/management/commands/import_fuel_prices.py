import csv
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from routeplanner.models import FuelStation
from routeplanner.services.geocode import STATE_CODES, lookup_city
from routeplanner.services.stations import clear_index


class Command(BaseCommand):
    help = "Import the fuel price CSV, keep one row per truck stop and place each at its city centre (offline)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("csv_path", nargs="?", default=str(Path(settings.BASE_DIR) / "data" / "fuel-prices.csv"))

    def handle(self, csv_path: str, **options: Any) -> None:
        cheapest: dict[int, dict[str, Any]] = {}  # a truck stop listed several times keeps its lowest price
        total = 0
        with Path(csv_path).open(newline="", encoding="utf-8-sig") as fh:
            for record in csv.DictReader(fh):
                total += 1
                state = record["State"].strip().upper()
                if state not in STATE_CODES:  # Canadian provinces
                    continue
                station_id, price = int(record["OPIS Truckstop ID"]), float(record["Retail Price"])
                if station_id not in cheapest or price < cheapest[station_id]["price"]:
                    cheapest[station_id] = {
                        "opis_id": station_id,
                        "name": record["Truckstop Name"].strip(),
                        "address": record["Address"].strip(),
                        "city": record["City"].strip(),
                        "state": state,
                        "price": price,
                        "rack_id": int(record["Rack ID"]) if record["Rack ID"].strip() else None,
                    }
        city_points: dict[tuple[str, str], tuple[float, float] | None] = {}
        stations, skipped = [], []
        for row in cheapest.values():
            key = (row["city"].lower(), row["state"])
            if key not in city_points:
                city_points[key] = lookup_city(row["city"], row["state"])
            point = city_points[key]
            if point is None:
                skipped.append(f"{row['city']}, {row['state']}")
                continue
            stations.append(FuelStation(lat=point[0], lon=point[1], **row))
        FuelStation.objects.all().delete()
        FuelStation.objects.bulk_create(stations, batch_size=1000)
        clear_index()
        summary = (
            f"{total} rows -> {len(cheapest)} unique US stations -> "
            f"{len(stations)} geocoded, {len(skipped)} skipped"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        if skipped:
            self.stdout.write("Skipped (city not in the offline place list): " + "; ".join(sorted(set(skipped))))
