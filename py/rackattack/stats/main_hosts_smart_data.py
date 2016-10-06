import logging
from rackattack.stats import config
from rackattack.stats import smartscanner
from rackattack.stats import elasticsearchdbwrapper


def main():
    config.configure_logger()
    db = elasticsearchdbwrapper.ElasticsearchDBWrapper()
    smart_scanner = SmartScanner()

    while True:
        try:
            #  do stuff
            break
        except elasticsearch.ConnectionTimeout:
            db.handle_disconnection()
        except elasticsearch.ConnectionError:
            db.handle_disconnection()
        except elasticsearch.exceptions.TransportError:
            db.handle_disconnection()
        except KeyboardInterrupt:
            break
        except Exception:
            msg = "Critical error, exiting.\n\n"
            logging.exception(msg)
            msg += traceback.format_exc()
            send_mail(msg)
            sys.exit(1)
    logging.info("Done.")

if __name__ == '__main__':
    main()
