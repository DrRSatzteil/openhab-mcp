from setuptools import setup, find_packages

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(
    name="openhab-mcp",
    version="0.1.0",
    packages=find_packages(),
    install_requires=requirements,
    author="Thomas Lauterbach",
    author_email="drrsatzteil@web.de",
    description="OpenHAB MCP - Management and Control Platform",
    long_description=open('README.md').read(),
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/openhab-mcp",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.8',
)
