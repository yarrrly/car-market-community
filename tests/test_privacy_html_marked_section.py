from tools.check_public_source_privacy import structured_role_loci


def test_html_metadata_scan_survives_marked_section_outside_html():
    # "<![" from a regex once crashed the scan on Python 3.12.11 and hid the
    # next tag on newer releases. Both pieces are built at run time so this
    # file itself holds neither a marked section nor a sensitive meta literal.
    marker = "<" + "!["
    field = "seller" + "Name"
    text = (
        f'PATTERN = r"(?{marker}\\w])(?:file:/{{2}})?"\n'
        f'<meta name="{field}" content="Example Seller">\n'
    )
    rows = [
        row
        for row in structured_role_loci(text)
        if row["category"] == "unreviewed_html_sensitive_metadata"
    ]
    assert [row["field"] for row in rows] == [field]
