"""Storage for student practice material, deliberately without a public URL."""
import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivatePracticeStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(file_permissions_mode=0o600, directory_permissions_mode=0o700)

    @property
    def base_location(self):
        return str(settings.PRIVATE_PRACTICE_ROOT)

    @property
    def location(self):
        if not self.base_location or not os.path.isabs(self.base_location):
            raise ImproperlyConfigured("PRIVATE_PRACTICE_ROOT must be a non-empty absolute path")
        root = os.path.realpath(self.base_location)
        # Neither tree may contain the other. Deployment web servers must also
        # avoid serving this path (including through aliases and symlinks).
        for public in (settings.MEDIA_ROOT, settings.STATIC_ROOT):
            if public:
                public_root = os.path.realpath(public)
                common = os.path.commonpath([root, public_root])
                if common in (root, public_root):
                    raise ImproperlyConfigured("PRIVATE_PRACTICE_ROOT must be separate from MEDIA_ROOT and STATIC_ROOT")
        return root

    def url(self, name):
        raise ValueError("Private practice files have no public URL.")


private_practice_storage = PrivatePracticeStorage()
