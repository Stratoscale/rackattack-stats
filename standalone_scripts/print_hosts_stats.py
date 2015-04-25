from rackattack import clientfactory


def main():
    client = clientfactory.factory()
    print client.call('admin__queryStatus')


if __name__ == '__main__':
    main()
