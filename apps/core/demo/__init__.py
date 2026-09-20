# Keep this package's __init__ empty and free of Django imports: `config.settings.dev`
# imports `apps.core.demo.data` while the settings module is still being read, long before
# `django.setup()` has populated the app registry. Anything touching a model here would
# break every `manage.py` invocation with AppRegistryNotReady.
