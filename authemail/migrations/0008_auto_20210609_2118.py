# Generated by Django 3.2 on 2021-06-09 21:18

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("authemail", "0007_auto_20210521_2226"),
    ]

    operations = [
        migrations.CreateModel(
            name="IpLocation",
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
                ("country", models.CharField(max_length=64)),
                ("country_code", models.CharField(max_length=10)),
                ("region", models.CharField(max_length=256)),
                ("city", models.CharField(max_length=256)),
            ],
        ),
        migrations.AddField(
            model_name="authauditlog",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="authemail.iplocation",
            ),
        ),
    ]
