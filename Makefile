RAP_URI = 10.16.3.1
MODULE_DIRNAME = $(shell basename `pwd`)
MODULE_NAME = ${subst -,.,$(MODULE_DIRNAME)}
EGG_BASENAME = ${MODULE_NAME}.egg
SERVICES_FILENAMES = $(shell find -maxdepth 1 -name "*.service" | sed 's/.\///g')
PYTHON_FILES = $(shell find py -name "*.py")
PYTHON_FILES_PATH_FROM_PY_ROOT = $(shell find py -name "*.py" | cut -d "/" -f 2-)
SERVICES_DEPLOYMENT_PATH = $(shell python get_system_setting.py serviceFilesDirPath)

all: check_convention build

check_convention:
	pep8 py --max-line-length=109

unittest:
	RACKATTACK_PROVIDER=asd@@asd@@asd UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=py python py/rackattack/stats/tests/insert_some_records.py

.PHONY: build
build: build/$(EGG_BASENAME)

build/${EGG_BASENAME}: ${PYTHON_FILES}
	mkdir -p $(@D)
	cd py; python -m upseto.packegg --entryPoint ${PYTHON_FILES_PATH_FROM_PY_ROOT} --output=../$@ --createDeps=../$@.dep --compile_pyc --joinPythonNamespaces

-include build/$(EGG_BASENAME).dep

install: build/$(EGG_BASENAME)
	-for _service in ${SERVICES_FILENAMES} ; do \
		sudo systemctl stop $$_service ; \
	done
	-sudo mkdir /usr/share/$(MODULE_NAME)
	sudo cp build/$(EGG_BASENAME) /usr/share/$(MODULE_NAME)
	for _service in ${SERVICES_FILENAMES} ; do \
		sudo sh -c "sed 's/<RAP_URI>/${RAP_URI}/g' $$_service | sed 's/<PYTHONPATH>/\/usr\/share\/$(MODULE_NAME)\/$(EGG_BASENAME)/g' > '${SERVICES_DEPLOYMENT_PATH}/$$_service'" ; \
		sudo systemctl enable $$_service ; \
	done

uninstall:
#	-sudo systemctl stop $(SERVICE_BASENAME)
#	-sudo systemctl disable $(SERVICE_BASENAME)
#	-sudo rm -fr /usr/lib/systemd/system/$(SERVICE_BASENAME)
	sudo rm -fr /usr/share/$(MODULE_NAME)

clean:
	-rm -rf build
