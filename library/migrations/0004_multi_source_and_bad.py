"""Multi-source support: split settings, add Game.source/is_bad, rename dlsite_id."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0003_appsettings_mtool_path_game_launch_with_mtool'),
    ]

    operations = [
        # --- Game ---
        # 1. Drop the unique constraint before renaming so we can add a
        #    composite (source, source_id) constraint afterwards.
        migrations.AlterField(
            model_name='game',
            name='dlsite_id',
            field=models.CharField(max_length=40),
        ),
        # 2. Rename the field (preserves data).
        migrations.RenameField(
            model_name='game',
            old_name='dlsite_id',
            new_name='source_id',
        ),
        # 3. Add the new source field defaulting to DLsite — existing rows
        #    keep working since they ARE DLsite imports.
        migrations.AddField(
            model_name='game',
            name='source',
            field=models.CharField(
                choices=[('dlsite', 'DLsite'), ('f95zone', 'F95Zone')],
                default='dlsite',
                max_length=20,
            ),
        ),
        # 4. Mark-as-bad flag.
        migrations.AddField(
            model_name='game',
            name='is_bad',
            field=models.BooleanField(default=False),
        ),
        # 5. New uniqueness on the source+id pair.
        migrations.AlterUniqueTogether(
            name='game',
            unique_together={('source', 'source_id')},
        ),

        # --- AppSettings ---
        migrations.RenameField(
            model_name='appsettings',
            old_name='default_install_root',
            new_name='dlsite_install_root',
        ),
        migrations.AddField(
            model_name='appsettings',
            name='f95zone_install_root',
            field=models.CharField(blank=True, max_length=1000),
        ),
    ]
