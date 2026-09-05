"""Talking to QuickBooks Desktop.

QuickBooks Desktop has no cloud API. Intuit's pages that read like endpoints
are qbXML *message schemas* for a Windows COM SDK, not HTTP. The only sanctioned
way in from outside the building is the **QuickBooks Web Connector**, and it
inverts control completely: QuickBooks calls us, on a schedule, over SOAP. We
never call QuickBooks.

That single fact shapes everything in this package:

  * We publish a SOAP endpoint. The Web Connector, running on a Windows machine
    beside the company file, polls it and asks "anything for me?"
  * We answer with one qbXML request at a time and it brings back the response
    on the next call. A conversation, not a function call.
  * So nothing here is synchronous. Every read is a mirror kept fresh by the
    last poll, and every write is queued and confirmed on a later one.

The pieces, in the order a request travels through them:

    qwc.py        the .qwc file the customer imports into the Web Connector
    soap.py       the SOAP envelope and the WSDL - transport, nothing more
    protocol.py   the eight-callback conversation, as a state machine
    qbxml.py      building qbXML requests and reading the responses
    sync.py       what to ask for, in what order, and where the answers go
    mirror.py     the answers, stored so every page can read them instantly

protocol.py knows nothing about HTTP and qbxml.py knows nothing about SOAP,
which is what makes both of them testable without a Windows machine, a
company file, or Intuit.
"""
