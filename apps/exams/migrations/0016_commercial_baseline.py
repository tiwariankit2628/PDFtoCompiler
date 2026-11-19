"""Conservative cutover: retain historic records and do not regrant used trials."""
from datetime import timedelta
from django.db import migrations
from django.utils import timezone


def baseline(apps, schema_editor):
    Series = apps.get_model("exams", "TestSeries")
    Slot = apps.get_model("exams", "SeriesSlot")
    Session = apps.get_model("exams", "ClassroomSession")
    Activation = apps.get_model("exams", "ClassroomActivation")
    Attempt = apps.get_model("exams", "ExamAttempt")
    Paper = apps.get_model("questions", "QuestionPaperUpload")
    Usage = apps.get_model("payments", "PracticeUsage")
    Use = apps.get_model("payments", "CreditUse")
    db = schema_editor.connection.alias

    for track, title in (("jee", "JEE test series"), ("neet", "NEET test series")):
        series, _ = Series.objects.using(db).get_or_create(track=track, defaults={"title": title})
        Slot.objects.using(db).bulk_create(
            [Slot(series=series, position=i) for i in range(1, 17)], ignore_conflicts=True)

    # The first free allowance is a lifetime rule. Historical evidence of a
    # PDF upload or monthly usage (including deleted uploads), classroom start,
    # or teacher session marks it used; never delete or alter the source rows.
    evidence = {
        "pdf": set(Paper.objects.using(db).filter(is_student_private=True).values_list("uploaded_by_id", flat=True))
               | set(Usage.objects.using(db).filter(pdf_uploads__gt=0).values_list("user_id", flat=True)),
        "classroom": set(Attempt.objects.using(db).filter(classroom_session__isnull=False).values_list("student_id", flat=True)),
        "activation": set(Session.objects.using(db).values_list("teacher_id", flat=True)),
    }
    for kind, user_ids in evidence.items():
        Use.objects.using(db).bulk_create([
            Use(user_id=uid, kind=kind, is_free=True, resource_key=f"legacy:{kind}:{uid}")
            for uid in user_ids
        ], ignore_conflicts=True, batch_size=500)

    # Existing sessions are grandfathered for a 24-hour transition from the
    # migration, without a retroactive debit. Existing active attempts remain
    # resumable even when a window ends; old result-release settings remain.
    now = timezone.now()
    groups = Session.objects.using(db).values_list("teacher_id", "exam_id").distinct().iterator()
    for teacher_id, exam_id in groups:
        activation = Activation.objects.using(db).create(teacher_id=teacher_id, exam_id=exam_id,
            starts_at=now, ends_at=now + timedelta(hours=24), is_legacy=True)
        Session.objects.using(db).filter(teacher_id=teacher_id, exam_id=exam_id).update(activation_id=activation.pk)


class Migration(migrations.Migration):
    dependencies = [
        ("exams", "0015_testseries_classroomactivation_and_more"),
        ("questions", "0018_question_target_type_questionpaperupload_target_type"),
        ("payments", "0008_packcheckout_creditgrant_packpayment_packrefund_and_more"),
        ("accounts", "0004_user_verification_required_user_verification_sent_at"),
    ]
    operations = [migrations.RunPython(baseline, migrations.RunPython.noop)]
