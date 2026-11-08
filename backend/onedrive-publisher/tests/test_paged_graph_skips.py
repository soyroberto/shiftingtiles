import pytest
from config import PublisherError
from graph import MAX_BYTES
from test_paged_graph import ROOT, client, response, source


def test_oversized_original_is_explicitly_counted_when_skipping_enabled():
    graph=client([response({'value':[source('large',size=MAX_BYTES+1),source('valid')]})])
    graph.skip_invalid=True
    result=graph.scan_page(ROOT)
    assert result['entries']==2 and len(result['rows'])==1
    assert result['skipped']=={'oversize':1}
    assert result['ids']==['large','valid']


@pytest.mark.parametrize('invalid',[-1,True,'123',None])
def test_skip_flag_does_not_suppress_malformed_size(invalid):
    graph=client([response({'value':[source(size=invalid)]})]);graph.skip_invalid=True
    with pytest.raises(PublisherError,match='file_size_bound'):graph.scan_page(ROOT)


def test_skip_flag_preserves_exact_maximum_size():
    graph=client([response({'value':[source(size=MAX_BYTES)]})]);graph.skip_invalid=True
    result=graph.scan_page(ROOT)
    assert len(result['rows'])==1 and result['skipped']=={}
