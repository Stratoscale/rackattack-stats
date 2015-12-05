RAP_URI = 10.16.3.1
EGG_BASENAME = ${SERVICE}.egg
PYTHON_FILES = $(shell find py -name "*.py")
PYTHON_FILES_PATH_FROM_PY_ROOT = $(shell find py -name "*.py" | cut -d "/" -f 2-)
SERVICES_DEPLOYMENT_PATH = $(shell python get_system_setting.py serviceFilesDirPath)

all: check_convention build

check_convention:
	pep8 py --max-line-length=109

unittest:
	UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=py python py/rackattack/stats/tests/insert_some_records.py

.PHONY: build
build: build/$(EGG_BASENAME)

build/${EGG_BASENAME}: validate_requirements ${PYTHON_FILES}
	mkdir -p $(@D)
	cd py; python -m upseto.packegg --entryPoint ${PYTHON_FILES_PATH_FROM_PY_ROOT} --output=../$@ --createDeps=../$@.dep --compile_pyc --joinPythonNamespaces

-include build/$(EGG_BASENAME).dep

.PHONY: install
install:
	make install_service SERVICE=rackattack-hosts-stats
	make install_service SERVICE=rackattack-allocatio-stats

.PHONY: install_service
install_service: clean build/$(EGG_BASENAME)
	-sudo systemctl stop ${SERVICE}.service
	-sudo mkdir /usr/share/${SERVICE}
	sudo cp build/$(EGG_BASENAME) /usr/share/${SERVICE}
	sudo sh -c "sed 's/<RAP_URI>/${RAP_URI}/g' ${SERVICE}.service | sed 's/<PYTHONPATH>/\/usr\/share\/$(SERVICE)\/$(EGG_BASENAME)/g' > '${SERVICES_DEPLOYMENT_PATH}/${SERVICE}.service'"
	sudo systemctl enable ${SERVICE}

uninstall:
	-sudo systemctl stop $(SERVICE).service
	-sudo systemctl disable $(SERVICE).service
	-sudo rm -fr ${SERVICES_DEPLOYMENT_PATH}/$(SERVICE).service
	sudo rm -fr /usr/share/$(SERVICE)

.PHONY: validate_requirements
validate_requirements:
	echo ${PYTHON_FILES}
	sudo pip install -r requirements.txt

clean:
	-rm -rf build
