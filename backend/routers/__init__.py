# routers/__init__.py
# Split into individual imports to avoid Python 3.11 relative-import race condition
from . import auth
from . import invoices
from . import customers
from . import payments
from . import cash
from . import advances
from . import stock
from . import reports
from . import export
from . import admin
from . import suppliers
