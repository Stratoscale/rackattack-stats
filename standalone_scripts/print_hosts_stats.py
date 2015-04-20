from rackattack import clientfactory


def main():
    client = clientfactory.factory()
    print rackattack_client.call('admin__queryStatus')


if __name__ == '__main__':
    main()
