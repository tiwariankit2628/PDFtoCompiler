#!/usr/bin/env python3
"""Anonymous connectivity checks only: no credentials, messages or payments."""
import smtplib
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT = 8


def smtp(port):
    connection = None
    try:
        if port == 587:
            connection = smtplib.SMTP("smtp.gmail.com", port, timeout=TIMEOUT)
            code, _ = connection.ehlo()
            if code != 250 or not connection.has_extn("starttls"):
                raise RuntimeError("STARTTLS was not offered")
            connection.starttls(context=ssl.create_default_context())
        else:
            connection = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=TIMEOUT, context=ssl.create_default_context())
        code, _ = connection.ehlo()
        if code != 250:
            raise RuntimeError("EHLO rejected")
        print(f"Gmail port {port}: verified TLS and SMTP EHLO succeeded; no authentication or email attempted.")
        return True
    except Exception as exc:
        print(f"Gmail port {port}: SMTP/TLS connection failed ({type(exc).__name__}); no authentication or email attempted.")
        return False
    finally:
        if connection:
            try:
                connection.quit()
            except Exception:
                connection.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def razorpay():
    url = "https://api.razorpay.com/v1/orders"
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=TIMEOUT) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code  # An unauthenticated 401/403 still proves an HTTP response over verified TLS.
        exc.close()
    except Exception as exc:
        print(f"Razorpay HTTPS: connection failed ({type(exc).__name__}); no authentication or order attempted.")
        return False
    if code not in (200, 401, 403, 405):
        print(f"Razorpay HTTPS: unexpected HTTP {code}; no authentication or order attempted.")
        return False
    print(f"Razorpay HTTPS: verified TLS and HTTP {code}; no authentication, payment or order attempted.")
    return True


def main():
    tls_587, tls_465, api = smtp(587), smtp(465), razorpay()
    print("Connectivity alone does not establish Gmail delivery or a successful Razorpay sandbox transaction.")
    return 0 if (tls_587 or tls_465) and api else 1


if __name__ == "__main__":
    sys.exit(main())
