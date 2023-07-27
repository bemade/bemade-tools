from decorator import decorate


def converter(method):
    wrapper = decorate(method, _converter)
    return wrapper


def _converter(method, self):
    to_convert = self.filtered(lambda r: not r.converted)
    results = method(to_convert)
    for i, converted in enumerate(results):
        to_convert[i].converted = converted
    return self.mapped('converted')
