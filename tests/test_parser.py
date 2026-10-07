from pullreqs.parser import parse_output

SAMPLE = """#f 178946 [Jens Kilian] Prepare for supporting Jeju AC/DC & DC/DC modules.

#f 178945 [Mohammad Mhalla] PSV-5371: obi: Implement diag :: RESOLVED: PSV-5371

178976 [Deepak Sharma] New deliverables from rf/master

#f 178738 [Marc-Oliver Straub] ist: immutable :: ConfigCache: dutboard ...
"""


def test_parse():
    prs = parse_output(SAMPLE)
    assert [p.native_id for p in prs] == ["178946", "178945", "178976", "178738"]
    assert prs[0].marker == "#f" and prs[2].marker == ""
    assert prs[0].author == "Jens Kilian"
    assert prs[1].title == "PSV-5371: obi: Implement diag"
    assert prs[1].description == "RESOLVED: PSV-5371"
