import builtins

# Ensure '_' exists even if gettext is not installed yet.
if '_' not in builtins.__dict__:
    builtins.__dict__['_'] = lambda s: s
