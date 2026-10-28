from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("signals", "0210_monitoring_report_outcomes")]

    operations = [
        migrations.AddField(
            model_name="signalreportmonitoringattempt",
            name="tracker_issue_close_processed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="signalreportmonitoringattempt",
            name="tracker_issue_close_next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="signalreportmonitoringattempt",
            name="close_pr_on_resolve",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="signalreporttrackerissue",
            name="close_comment_posted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
