import sys
import threading
import warnings
from collections import Counter, OrderedDict, defaultdict
from functools import partial

from django.core.exceptions import AppRegistryNotReady, ImproperlyConfigured
from django.utils import lru_cache

from .config import AppConfig


class Apps(object):
    """
    A registry that stores the configuration of installed applications.

    It also keeps track of models eg. to provide reverse-relations.
    """

    def __init__(self, installed_apps=()):
        # installed_apps is set to None when creating the master registry
        # because it cannot be populated at that point. Other registries must
        # provide a list of installed apps and are populated immediately.
        if installed_apps is None and hasattr(sys.modules[__name__], 'apps'):
            raise RuntimeError("You must supply an installed_apps argument.")

        # Mapping of app labels => model names => model classes. Every time a
        # model is imported, ModelBase.__new__ calls apps.register_model which
        # creates an entry in all_models. All imported models are registered,
        # regardless of whether they're defined in an installed application
        # and whether the registry has been populated. Since it isn't possible
        # to reimport a module safely (it could reexecute initialization code)
        # all_models is never overridden or reset.
        self.all_models = defaultdict(OrderedDict)

        # Mapping of labels to AppConfig instances for installed apps.
        self.app_configs = OrderedDict()

        # Stack of app_configs. Used to store the current state in
        # set_available_apps and set_installed_apps.
        self.stored_app_configs = []

        # Whether the registry is populated.
        self.apps_ready = self.models_ready = self.ready = False

        # Lock for thread-safe population.
        self._lock = threading.Lock()

        # Maps ("app_label", "modelname") tuples to lists of functions to be
        # called when the corresponding model is ready. Used by this class's
        # `lazy_model_operation()` and `do_pending_operations()` methods.
        self._pending_operations = defaultdict(list)

        # Populate apps and models, unless it's the master registry.
        if installed_apps is not None:
            self.populate(installed_apps)

    def populate(self, installed_apps=None):
        """
        Loads application configurations and models.

        This method imports each application module and then each model module.

        It is thread safe and idempotent, but not reentrant.
        """
        if self.ready:
            return

        # populate() might be called by two threads in parallel on servers
        # that create threads before initializing the WSGI callable.
        with self._lock:
            if self.ready:
                return

            # app_config should be pristine, otherwise the code below won't
            # guarantee that the order matches the order in INSTALLED_APPS.
            if self.app_configs:
                raise RuntimeError("populate() isn't reentrant")

            # Load app configs and app modules.
            for entry in installed_apps:
                if isinstance(entry, AppConfig):
                    app_config = entry
                else:
                    app_config = AppConfig.create(entry)
                if app_config.label in self.app_configs:
                    raise ImproperlyConfigured(
                        "Application labels aren't unique, "
                        "duplicates: %s" % app_config.label)

                self.app_configs[app_config.label] = app_config

            # Check for duplicate app names.
            counts = Counter(
                app_config.name for app_config in self.app_configs.values())
            duplicates = [
                name for name, count in counts.most_common() if count > 1]
            if duplicates:
                raise ImproperlyConfigured(
                    "Application names aren't unique, "
                    "duplicates: %s" % ", ".join(duplicates))

            self.apps_ready = True

            # Load models.
            for app_config in self.app_configs.values():
                all_models = self.all_models[app_config.label]
                app_config.import_models(all_models)

            self.clear_cache()

            self.models_ready = True

            for app_config in self.get_app_configs():
                app_config.ready()

            self.ready = True

    def check_apps_ready(self):
        """
        Raises an exception if all apps haven't been imported yet.
        """
        if not self.apps_ready:
            raise AppRegistryNotReady("Apps aren't loaded yet.")

    def check_models_ready(self):
        """
        Raises an exception if all models haven't been imported yet.
        """
        if not self.models_ready:
            raise AppRegistryNotReady("Models aren't loaded yet.")

    def get_app_configs(self):
        """
        Imports applications and returns an iterable of app configs.
        """
        self.check_apps_ready()
        return self.app_configs.values()

    def get_app_config(self, app_label):
        """
        Imports applications and returns an app config for the given label.

        Raises LookupError if no application exists with this label.
        """
        self.check_apps_ready()
        try:
            return self.app_configs[app_label]
        except KeyError:
            message = "No installed app with label '%s'." % app_label
            for app_config in self.get_app_configs():
                if app_config.name == app_label:
                    message += " Did you mean '%s'?" % app_config.label
                    break
            raise LookupError(message)

    # This method is performance-critical at least for Django's test suite.
    @lru_cache.lru_cache(maxsize=None)
    def get_models(self, include_auto_created=False,
                   include_deferred=False, include_swapped=False):
        """
        Returns a list of all installed models.

        By default, the following models aren't included:

        - auto-created models for many-to-many relations without
          an explicit intermediate table,
        - models created to satisfy deferred attribute queries,
        - models that have been swapped out.

        Set the corresponding keyword argument to True to include such models.
        """
        self.check_models_ready()

        result = []
        for app_config in self.app_configs.values():
            result.extend(list(app_config.get_models(
                include_auto_created, include_deferred, include_swapped)))
        return result

    def get_model(self, app_label, model_name=None):
        """
        Returns the model matching the given app_label and model_name.

        As a shortcut, this function also accepts a single argument in the
        form <app_label>.<model_name>.

        model_name is case-insensitive.

        Raises LookupError if no application exists with this label, or no
        model exists with this name in the application. Raises ValueError if
        called with a single argument that doesn't contain exactly one dot.
        """
        self.check_models_ready()
        if model_name is None:
            app_label, model_name = app_label.split('.')
        return self.get_app_config(app_label).get_model(model_name.lower())

    def register_model(self, app_label, model):
        # Since this method is called when models are imported, it cannot
        # perform imports because of the risk of import loops. It mustn't
        # call get_app_config().
        model_name = model._meta.model_name
        app_models = self.all_models[app_label]
        if model_name in app_models:
            if (model.__name__ == app_models[model_name].__name__ and
                    model.__module__ == app_models[model_name].__module__):
                warnings.warn(
                    "Model '%s.%s' was already registered. "
                    "Reloading models is not advised as it can lead to inconsistencies, "
                    "most notably with related models." % (app_label, model_name),
                    RuntimeWarning, stacklevel=2)
            else:
                raise RuntimeError(
                    "Conflicting '%s' models in application '%s': %s and %s." %
                    (model_name, app_label, app_models[model_name], model))
        app_models[model_name] = model
        self.do_pending_operations(model)
        self.clear_cache()

    def is_installed(self, app_name):
        """
        Checks whether an application with this name exists in the registry.

        app_name is the full name of the app eg. 'django.contrib.admin'.
        """
        self.check_apps_ready()
        return any(ac.name == app_name for ac in self.app_configs.values())

    def get_containing_app_config(self, object_name):
        """
        Look for an app config containing a given object.

        object_name is the dotted Python path to the object.

        Returns the app config for the inner application in case of nesting.
        Returns None if the object isn't in any registered app config.
        """
        self.check_apps_ready()
        candidates = []
        for app_config in self.app_configs.values():
            if object_name.startswith(app_config.name):
                subpath = object_name[len(app_config.name):]
                if subpath == '' or subpath[0] == '.':
                    candidates.append(app_config)
        if candidates:
            return sorted(candidates, key=lambda ac: -len(ac.name))[0]

    def get_registered_model(self, app_label, model_name):
        """
        Similar to get_model(), but doesn't require that an app exists with
        the given app_label.

        It's safe to call this method at import time, even while the registry
        is being populated.
        """
        model = self.all_models[app_label].get(model_name.lower())
        if model is None:
            raise LookupError(
                "Model '%s.%s' not registered." % (app_label, model_name))
        return model

    def set_available_apps(self, available):
        """
        Restricts the set of installed apps used by get_app_config[s].

        available must be an iterable of application names.

        set_available_apps() must be balanced with unset_available_apps().

        Primarily used for performance optimization in TransactionTestCase.

        This method is safe is the sense that it doesn't trigger any imports.
        """
        available = set(available)
        installed = set(app_config.name for app_config in self.get_app_configs())
        if not available.issubset(installed):
            raise ValueError("Available apps isn't a subset of installed "
                "apps, extra apps: %s" % ", ".join(available - installed))

        self.stored_app_configs.append(self.app_configs)
        self.app_configs = OrderedDict(
            (label, app_config)
            for label, app_config in self.app_configs.items()
            if app_config.name in available)
        self.clear_cache()

    def unset_available_apps(self):
        """
        Cancels a previous call to set_available_apps().
        """
        self.app_configs = self.stored_app_configs.pop()
        self.clear_cache()

    def set_installed_apps(self, installed):
        """
        Enables a different set of installed apps for get_app_config[s].

        installed must be an iterable in the same format as INSTALLED_APPS.

        set_installed_apps() must be balanced with unset_installed_apps(),
        even if it exits with an exception.

        Primarily used as a receiver of the setting_changed signal in tests.

        This method may trigger new imports, which may add new models to the
        registry of all imported models. They will stay in the registry even
        after unset_installed_apps(). Since it isn't possible to replay
        imports safely (eg. that could lead to registering listeners twice),
        models are registered when they're imported and never removed.
        """
        if not self.ready:
            raise AppRegistryNotReady("App registry isn't ready yet.")
        self.stored_app_configs.append(self.app_configs)
        self.app_configs = OrderedDict()
        self.apps_ready = self.models_ready = self.ready = False
        self.clear_cache()
        self.populate(installed)

    def unset_installed_apps(self):
        """
        Cancels a previous call to set_installed_apps().
        """
        self.app_configs = self.stored_app_configs.pop()
        self.apps_ready = self.models_ready = self.ready = True
        self.clear_cache()

    def clear_cache(self):
        """
        Clears all internal caches, for methods that alter the app registry.

        This is mostly used in tests.
        """
        # Call expire cache on each model. This will purge
        # the relation tree and the fields cache.
        self.get_models.cache_clear()
        if self.ready:
            # Circumvent self.get_models() to prevent that the cache is refilled.
            # This particularly prevents that an empty value is cached while cloning.
            for app_config in self.app_configs.values():
                for model in app_config.get_models(include_auto_created=True):
                    model._meta._expire_cache()

    def lazy_model_operation(self, function, *model_keys):
        """
        Take a function and a number of ("app_label", "modelname") tuples, and
        when all the corresponding models have been imported and registered,
        call the function with the model classes as its arguments.

        The function passed to this method must accept exactly n models as
        arguments, where n=len(model_keys).
        """
        # If this function depends on more than one model, we recursively turn
        # it into a chain of functions that accept a single model argument and
        # pass each in turn to lazy_model_operation.
        model_key, more_models = model_keys[0], model_keys[1:]
        if more_models:
            supplied_fn = function

            def function(model):
                next_function = partial(supplied_fn, model)
                self.lazy_model_operation(next_function, *more_models)

        # If the model is already loaded, pass it to the function immediately.
        # Otherwise, delay execution until the class is prepared.
        try:
            model_class = self.get_registered_model(*model_key)
        except LookupError:
            self._pending_operations[model_key].append(function)
        else:
            function(model_class)

    def do_pending_operations(self, model):
        """
        Take a newly-prepared model and pass it to each function waiting for
        it. This is called at the very end of `Apps.register_model()`.
        """
        key = model._meta.app_label, model._meta.model_name
        for function in self._pending_operations.pop(key, []):
            function(model)

apps = Apps(installed_apps=None)
