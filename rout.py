
import importlib, sys
m = importlib.import_module('server')
print("Loaded file:", m.__file__)
print("Routes:")
for rule in m.app.url_map.iter_rules():
    print(f"{rule}  -> methods={sorted(rule.methods)}")
