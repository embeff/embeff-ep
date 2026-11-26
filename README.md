# embeff-ep

Python package for the [embeff ExecutionPlatform](https://embeff.com): the Robot Framework
library `EPLibrary`, the pytest plugin and the `ep-config` command line tool.

Documentation: <https://ep-docs.embeff.com>

## Installation

Install the release that matches your ExecutionPlatform version (major.minor must match
the EP):

```
pip install "embeff-ep[robot,pytest] @ git+https://github.com/embeff/embeff-ep@v<version>"
```

The extras are `robot` (Robot Framework), `pytest` and `all`. The software package
[embeff-ep-software-package](https://github.com/embeff/embeff-ep-software-package) pins the
matching version in its `requirements.txt`.

## Usage

### Robot Framework

```robotframework
*** Settings ***
Library    EPLibrary    ${ep_url}    ${ep_binary_dir}/gpio.ep-config.zip
```

### pytest

The plugin registers itself when the package is installed. A test module gets its EP class from
the module generated into each ep-config. `EP.fixture()` creates the module-scoped fixture that
opens the EP, and the same class annotates the parameter for code completion:

```python
from gpio.test import EP  # gpio.ep-config.zip on the pytest pythonpath

ep = EP.fixture()


def test_square(ep: EP):
    assert ep.DUT.invoke("square", "3") == 9
```

### ep-config CLI

```
ep-config info build/gpio.ep-config.zip
ep-config build --dut <DUT> --version <version> --output-folder build my_config.ep-config.yaml
```

## License

Apache-2.0, see `LICENSE`.
