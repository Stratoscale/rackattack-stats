import logging
import traceback
import elasticsearch
from rackattack.stats import logconfig
from rackattack.stats import smartscanner
from rackattack.stats import elasticsearchdbwrapper


def main():
    logconfig.configure_logger()
    db = elasticsearchdbwrapper.ElasticsearchDBWrapper()
    smart_scanner = smartscanner.SmartScanner(db)

    while True:
        try:
            logging.info("Starting SMART scan loop...")
            smart_scanner.run()
            break
        except elasticsearch.ConnectionTimeout:
            db.handle_disconnection()
        except elasticsearch.ConnectionError:
            db.handle_disconnection()
        except elasticsearch.exceptions.TransportError:
            db.handle_disconnection()
        except KeyboardInterrupt:
            break
        except:
            logging.error("Critical error, exiting")
            raise

if __name__ == '__main__':
    main()
