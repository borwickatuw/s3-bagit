# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `verify` now reads each payload/tag file once regardless of how many
  manifest algorithms the bag carries (was Nx GETs for N-algorithm
  bags). Single-algorithm bags unchanged. Operators watching
  CloudTrail / S3 access logs will see the GET count drop for
  multi-algorithm bags.
