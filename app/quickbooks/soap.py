"""SOAP, and only SOAP.

The Web Connector is a SOAP 1.1 client and will not speak anything else. It
fetches the WSDL once, then POSTs envelopes. Everything below is transport:
unwrap the envelope, hand the arguments to protocol.Service, wrap the answer
back up.

Written by hand against the published contract rather than with a SOAP stack,
because the whole contract is eight methods of strings and integers, and a
framework to marshal that would be more code than this file - and would still
need the same WSDL served at the same URL.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

NS = "http://developer.intuit.com/"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"

_OPS = (
    "serverVersion", "clientVersion", "authenticate", "sendRequestXML",
    "receiveResponseXML", "connectionError", "getLastError", "closeConnection",
)

# What each operation takes, in the order the connector sends it. The names
# matter: the connector sends named child elements, not positional ones.
_ARGS = {
    "serverVersion": (),
    "clientVersion": ("strVersion",),
    "authenticate": ("strUserName", "strPassword"),
    "sendRequestXML": ("ticket", "strHCPResponse", "strCompanyFileName",
                       "qbXMLCountry", "qbXMLMajorVers", "qbXMLMinorVers"),
    "receiveResponseXML": ("ticket", "response", "hresult", "message"),
    "connectionError": ("ticket", "hresult", "message"),
    "getLastError": ("ticket",),
    "closeConnection": ("ticket",),
}


class SoapError(ValueError):
    """The request was not a SOAP envelope we recognise."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_request(xml: str) -> tuple[str, dict[str, str]]:
    """Pull the operation name and its arguments out of a SOAP envelope."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SoapError(f"not XML: {exc}") from exc

    body = next((el for el in root if _local(el.tag) == "Body"), None)
    if body is None:
        raise SoapError("no SOAP Body")
    call = next(iter(body), None)
    if call is None:
        raise SoapError("empty SOAP Body")

    operation = _local(call.tag)
    if operation not in _ARGS:
        raise SoapError(f"unknown operation {operation}")

    args = {_local(child.tag): (child.text or "") for child in call}
    return operation, args


def _escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def build_response(operation: str, value) -> str:
    """Wrap a return value in the envelope shape the connector expects.

    `authenticate` is the odd one: it returns an array of strings, which on
    the wire is repeated <string> elements inside the result. Returning a
    single string there makes the connector report a type mismatch and stop.
    """
    if isinstance(value, (list, tuple)):
        inner = "".join(f"<string>{_escape(v)}</string>" for v in value)
        result = f'<{operation}Result xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{inner}</{operation}Result>'
    else:
        result = f"<{operation}Result>{_escape(value)}</{operation}Result>"

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        "<soap:Body>"
        f'<{operation}Response xmlns="{NS}">{result}</{operation}Response>'
        "</soap:Body></soap:Envelope>"
    )


def fault(message: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><soap:Fault>"
        "<faultcode>soap:Client</faultcode>"
        f"<faultstring>{_escape(message)}</faultstring>"
        "</soap:Fault></soap:Body></soap:Envelope>"
    )


def dispatch(service, operation: str, args: dict[str, str]):
    """Call the right method on protocol.Service with the right arguments."""
    def _int(name: str) -> Optional[int]:
        raw = (args.get(name) or "").strip()
        try:
            return int(raw)
        except ValueError:
            return None

    if operation == "serverVersion":
        return service.server_version()
    if operation == "clientVersion":
        return service.client_version(args.get("strVersion", ""))
    if operation == "authenticate":
        return service.authenticate(args.get("strUserName", ""),
                                    args.get("strPassword", ""))
    if operation == "sendRequestXML":
        return service.send_request_xml(
            args.get("ticket", ""), args.get("strHCPResponse", ""),
            args.get("strCompanyFileName", ""), args.get("qbXMLCountry", ""),
            _int("qbXMLMajorVers"), _int("qbXMLMinorVers"),
        )
    if operation == "receiveResponseXML":
        return service.receive_response_xml(
            args.get("ticket", ""), args.get("response", ""),
            args.get("hresult", ""), args.get("message", ""),
        )
    if operation == "connectionError":
        return service.connection_error(args.get("ticket", ""),
                                        args.get("hresult", ""),
                                        args.get("message", ""))
    if operation == "getLastError":
        return service.get_last_error(args.get("ticket", ""))
    if operation == "closeConnection":
        return service.close_connection(args.get("ticket", ""))
    raise SoapError(f"unhandled operation {operation}")


# --- the WSDL --------------------------------------------------------------

def _message(name: str, parts: str) -> str:
    return f'<message name="{name}">{parts}</message>'


def wsdl(endpoint: str) -> str:
    """The contract, served at the endpoint the .qwc file points at.

    The connector fetches this before it will talk to us, and checks that the
    operations it needs are all present. It is static apart from the address,
    which has to be the URL the connector itself reached - not the one we
    think we are on, because behind a proxy those differ and the connector
    posts to whatever this says.
    """
    types = (
        '<types><xsd:schema elementFormDefault="qualified" '
        f'targetNamespace="{NS}" xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        '<xsd:complexType name="ArrayOfString">'
        '<xsd:sequence><xsd:element maxOccurs="unbounded" minOccurs="0" '
        'name="string" nillable="true" type="xsd:string"/></xsd:sequence>'
        "</xsd:complexType></xsd:schema></types>"
    )

    parts = []
    for op in _OPS:
        request = "".join(
            f'<part name="{arg}" type="xsd:{"int" if arg.endswith("Vers") else "string"}"/>'
            for arg in _ARGS[op]
        )
        kind = "tns:ArrayOfString" if op == "authenticate" else "xsd:string"
        if op == "receiveResponseXML":
            kind = "xsd:int"
        parts.append(_message(f"{op}SoapIn", request))
        parts.append(_message(f"{op}SoapOut",
                              f'<part name="{op}Result" type="{kind}"/>'))

    operations = "".join(
        f'<operation name="{op}">'
        f'<input message="tns:{op}SoapIn"/><output message="tns:{op}SoapOut"/>'
        "</operation>"
        for op in _OPS
    )
    bindings = "".join(
        f'<operation name="{op}">'
        f'<soap:operation soapAction="{NS}{op}" style="document"/>'
        '<input><soap:body use="literal"/></input>'
        '<output><soap:body use="literal"/></output>'
        "</operation>"
        for op in _OPS
    )

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"'
        ' xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        f' xmlns:tns="{NS}" targetNamespace="{NS}"'
        ' name="QBWebConnectorSvc">'
        f"{types}"
        f"{''.join(parts)}"
        f'<portType name="QBWebConnectorSvcSoap">{operations}</portType>'
        '<binding name="QBWebConnectorSvcSoap" type="tns:QBWebConnectorSvcSoap">'
        '<soap:binding style="document" '
        'transport="http://schemas.xmlsoap.org/soap/http"/>'
        f"{bindings}</binding>"
        '<service name="QBWebConnectorSvc">'
        '<port binding="tns:QBWebConnectorSvcSoap" name="QBWebConnectorSvcSoap">'
        f'<soap:address location="{_escape(endpoint)}"/>'
        "</port></service></definitions>"
    )
