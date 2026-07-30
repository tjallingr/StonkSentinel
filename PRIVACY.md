# Privacy Policy

**Application:** stonksentinel (self-hosted personal finance overview)

## Summary

This is a personal, single-user application. It is run by only me, on
my Raspberry Pi, to view my own financial accounts.
There are no other users, no third-party recipients, and no commercial use.

## What data is processed

- Account identifiers, product names and IBANs for bank accounts belonging to me
- Account balances and balance reference dates
- Investment positions: instrument identifiers, quantities, prices and market values
- Deposits and withdrawals into my own investment account
- Publicly published foreign exchange reference rates

## Whose data

Only my own accounts. Access is technically restricted to accounts I have personally linked and authenticated. The application has no facility for any other person to connect an account.

## Where data is stored

In a local SQLite database on a single self-hosted server on my private
network. Backups are encrypted at rest before leaving that machine. The data is not
stored in any shared, multi-tenant or publicly reachable system.

## Who data is shared with

Nobody. There are no analytics, no advertising, no telemetry, no third-party
processors, and no data sales or transfers. The dashboard makes zero outbound
requests to third parties when rendered.

## Third parties the application connects to

Data is retrieved from, but not sent to, the following, each acting under my own explicit consent:

| Provider | Purpose | Direction |
|---|---|---|
| Enable Banking | PSD2 account information (balances) | read |
| Saxo Bank OpenAPI | investment balances and positions | read |
| European Central Bank | published FX reference rates | read |

No personal data is transmitted to any party other than the authentication
credentials required to read my own accounts.

## Legal basis

Processing of my own personal data for my own purely
personal purposes. Where consent is required to access payment account information,
it is given by me directly to their own bank through Strong Customer
Authentication, and can be withdrawn at any time via the bank or by deleting the
session.

## Retention

Balance and position snapshots are retained indefinitely, because historical
records are the entire purpose of the application. All data can be erased at any
time by deleting the database file. Access tokens and bank consents expire
automatically and are deleted when superseded.

## Rights

As the sole data subject is also myself, rights of access, rectification,
erasure, restriction, portability and objection are exercised directly by
deleting or editing local files. Requests concerning any bank's own processing
should be directed to that bank.

## Security

- The service listens on the loopback interface only and is not exposed to the
  public internet
- Remote access is via an authenticated private network overlay with TLS
- Credentials and private keys are stored outside version control with restricted
  file permissions
- API access is read-only; the application cannot initiate payments or place trades

## Changes

Any changes will be published at this URL.
