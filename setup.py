from setuptools import find_packages, setup

setup(
    name="kitchen-scanner",
    version="0.1.0",
    description="Kitchen barcode scanner service",
    author="Blaž Bregar",
    author_email="blaz@aklaro.si",
    packages=find_packages(),
    py_modules=["main"],
    install_requires=[
        "evdev",
        "httpx",
        "tenacity",
    ],
    python_requires=">=3.9",
)
