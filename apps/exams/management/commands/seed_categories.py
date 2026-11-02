from django.core.management.base import BaseCommand
from apps.exams.models import Category


class Command(BaseCommand):
    help = "Seeds default examination categories dynamically into database."

    DEFAULT_CATEGORIES = [
        {
            "name": "Competitive Examinations",
            "slug": "competitive-examinations",
            "description": "UPSC CSE, SSC CGL/CHSL, Banking (IBPS, SBI PO), Railways RRB, and State Public Service Commissions.",
            "icon": "bi-trophy",
            "display_order": 1,
            "seo_title": "Competitive Exam Mock Tests & Online CBT Practice",
            "seo_description": "Practice national and state-level competitive examinations with real-time ranking and percentiles.",
        },
        {
            "name": "Engineering & Medical Entrance",
            "slug": "engineering-medical-entrance",
            "description": "JEE Main, JEE Advanced, NEET-UG, BITSAT, VITEEE, and GATE entrance examinations with formula rendering.",
            "icon": "bi-cpu",
            "display_order": 2,
            "seo_title": "JEE Main, NEET & GATE Online Mock Assessment Engine",
            "seo_description": "Simulate exact NTA testing pattern with negative marking and detailed solution analysis.",
        },
        {
            "name": "School Examinations",
            "slug": "school-examinations",
            "description": "CBSE, ICSE, and State Board secondary/senior secondary mock tests, periodic assessments, and chapter quizzes.",
            "icon": "bi-mortarboard",
            "display_order": 3,
            "seo_title": "Board Exam Practice & School Assessments",
            "seo_description": "Curriculum-aligned chapter-wise and term-end online tests for school students.",
        },
        {
            "name": "College & University Exams",
            "slug": "college-university-exams",
            "description": "Semester exams, mid-term tests, degree assessments, and campus placement screening tests.",
            "icon": "bi-building",
            "display_order": 4,
            "seo_title": "University & Semester Online Examination Portal",
            "seo_description": "Internal and semester examination management for universities and autonomous colleges.",
        },
        {
            "name": "Government & Recruitment",
            "slug": "government-recruitment",
            "description": "Defense (NDA, CDS, AFCAT), Police recruitment, Teaching eligibility (CTET, State TET), and PSU hiring.",
            "icon": "bi-shield-check",
            "display_order": 5,
            "seo_title": "Government Job CBT Recruitment Mock Tests",
            "seo_description": "Authentic examination simulation for defense, police, teaching, and PSU employment tests.",
        },
        {
            "name": "Coding & Technical Assessments",
            "slug": "coding-technical-assessments",
            "description": "Data Structures, Algorithms, Python, Java, C++, SQL queries, and technical hiring assessments.",
            "icon": "bi-code-slash",
            "display_order": 6,
            "seo_title": "Technical Skill Assessments & Coding Tests",
            "seo_description": "Evaluate coding concepts, problem solving, algorithms, and system fundamentals.",
        },
        {
            "name": "Custom Mock Examinations",
            "slug": "custom-mock-examinations",
            "description": "Tailored assessment tests crafted by coaching institutes, private tutors, and educator faculties.",
            "icon": "bi-sliders",
            "display_order": 7,
            "seo_title": "Custom Test Series & Institute Assessment Papers",
            "seo_description": "Bespoke mock test series with institutional cutoffs and negative scoring.",
        },
    ]

    def handle(self, *args, **options):
        created_count = 0
        for item in self.DEFAULT_CATEGORIES:
            cat, created = Category.objects.get_or_create(
                slug=item["slug"],
                defaults=item,
            )
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"Created category: {cat.name}"))
            else:
                self.stdout.write(f"Category '{cat.name}' already exists.")

        self.stdout.write(self.style.SUCCESS(f"Seeding completed. {created_count} categories created."))

