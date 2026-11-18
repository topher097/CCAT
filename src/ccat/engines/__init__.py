"""External validation engine adapters.

ZeroBounce email validation and Nominatim address validation.  These
adapters wrap the third-party APIs (``zerobouncesdk``, Nominatim's
REST endpoint) behind plain functions; the screening stage imports them
lazily and maps failures to :class:`ccat.screening.ScreeningError`.
"""
