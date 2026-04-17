# Internal Service Contracts

FlowPilot now separates backend concerns into explicit service contracts:

- `wallet.balance.get` -> wallet state lookup (WalletRepository)
- `wallet.credit.topup` -> webhook-driven wallet credit
- `wallet.debit.payout` -> atomic wallet debit before execution
- `kyc.verify.level1` -> Monnify BVN/NIN verification
- `kyc.attach_bvn_reserved_account` -> Monnify reserved account update
- `payment.account.validate` -> Monnify account name lookup
- `payment.transfer.single` -> Monnify single disbursement
- `payment.transfer.status` -> Monnify disbursement status
- `compliance.travel_rule.validate` -> hard-block validation + persistence

HTTP routes for payment-service contracts:

- `POST /api/v1/internal/payment/account/validate`
- `POST /api/v1/internal/payment/transfer/single`
- `POST /api/v1/webhooks/monnify`
