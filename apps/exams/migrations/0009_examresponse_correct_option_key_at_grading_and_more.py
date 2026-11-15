# Preserve classroom grading-time key and choice labels without backfilling old attempts.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('exams', '0008_classroomsession_classroomparticipant_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='examresponse',
            name='correct_option_key_at_grading',
            field=models.CharField(blank=True, editable=False, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name='examresponse',
            name='key_confirmed_at_grading',
            field=models.BooleanField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='examresponse',
            name='selected_option_key_at_grading',
            field=models.CharField(blank=True, editable=False, max_length=10, null=True),
        ),
    ]
