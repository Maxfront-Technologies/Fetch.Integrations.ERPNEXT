from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in fetch_einvoice/__init__.py
from fetch_einvoice import __version__ as version

setup(
	name="fetch_einvoice",
	version=version,
	description="Send ERPNext Sales Invoices and payments to FETCH for NRS e-invoicing, and receive the IRN back",
	author="Maxfront",
	author_email="",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
