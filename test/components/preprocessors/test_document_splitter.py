# SPDX-FileCopyrightText: 2022-present deepset GmbH <info@deepset.ai>
#
# SPDX-License-Identifier: Apache-2.0
import re

import pytest

from haystack import Document
from haystack.components.preprocessors import DocumentSplitter
from haystack.utils import deserialize_callable, serialize_callable


# custom split function for testing
def custom_split(text):
    return text.split(".")


def merge_documents(documents):
    """Merge a list of doc chunks into a single doc by concatenating their content, eliminating overlapping content."""
    sorted_docs = sorted(documents, key=lambda doc: doc.meta["split_idx_start"])
    merged_text = ""
    last_idx_end = 0
    for doc in sorted_docs:
        start = doc.meta["split_idx_start"]  # start of the current content

        # if the start of the current content is before the end of the last appended content, adjust it
        if start < last_idx_end:
            start = last_idx_end

        # append the non-overlapping part to the merged text
        merged_text += doc.content[start - doc.meta["split_idx_start"] :]

        # update the last end index
        last_idx_end = doc.meta["split_idx_start"] + len(doc.content)

    return merged_text


class TestDocumentSplitter:
    def test_non_text_document(self):
        with pytest.raises(
            ValueError, match="DocumentSplitter only works with text documents but content for document ID"
        ):
            splitter = DocumentSplitter()
            splitter.run(documents=[Document()])
            assert "DocumentSplitter only works with text documents but content for document ID" in caplog.text

    def test_single_doc(self):
        with pytest.raises(TypeError, match="DocumentSplitter expects a List of Documents as input."):
            splitter = DocumentSplitter()
            splitter.run(documents=Document())

    def test_empty_list(self):
        splitter = DocumentSplitter()
        res = splitter.run(documents=[])
        assert res == {"documents": []}

    def test_unsupported_split_by(self):
        with pytest.raises(
            ValueError, match="split_by must be one of 'function', 'word', 'sentence', 'page', 'passage' or 'line'."
        ):
            DocumentSplitter(split_by="unsupported")

    def test_unsupported_split_length(self):
        with pytest.raises(ValueError, match="split_length must be greater than 0."):
            DocumentSplitter(split_length=0)

    def test_unsupported_split_overlap(self):
        with pytest.raises(ValueError, match="split_overlap must be greater than or equal to 0."):
            DocumentSplitter(split_overlap=-1)

    def test_split_by_word(self):
        splitter = DocumentSplitter(split_by="word", split_length=10)
        text = "This is a text with some words. There is a second sentence. And there is a third sentence."
        result = splitter.run(documents=[Document(content=text)])
        docs = result["documents"]
        assert len(docs) == 2
        assert docs[0].content == "This is a text with some words. There is a "
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)
        assert docs[1].content == "second sentence. And there is a third sentence."
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)

    def test_split_by_word_with_threshold(self):
        splitter = DocumentSplitter(split_by="word", split_length=15, split_threshold=10)
        result = splitter.run(
            documents=[
                Document(
                    content="This is a text with some words. There is a second sentence. And there is a third sentence."
                )
            ]
        )
        assert len(result["documents"]) == 1
        assert (
            result["documents"][0].content
            == "This is a text with some words. There is a second sentence. And there is a third sentence."
        )

    def test_split_by_word_multiple_input_docs(self):
        splitter = DocumentSplitter(split_by="word", split_length=10)
        text1 = "This is a text with some words. There is a second sentence. And there is a third sentence."
        text2 = "This is a different text with some words. There is a second sentence. And there is a third sentence. And there is a fourth sentence."
        result = splitter.run(documents=[Document(content=text1), Document(content=text2)])
        docs = result["documents"]
        assert len(docs) == 5
        # doc 0
        assert docs[0].content == "This is a text with some words. There is a "
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text1.index(docs[0].content)
        # doc 1
        assert docs[1].content == "second sentence. And there is a third sentence."
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text1.index(docs[1].content)
        # doc 2
        assert docs[2].content == "This is a different text with some words. There is "
        assert docs[2].meta["split_id"] == 0
        assert docs[2].meta["split_idx_start"] == text2.index(docs[2].content)
        # doc 3
        assert docs[3].content == "a second sentence. And there is a third sentence. And "
        assert docs[3].meta["split_id"] == 1
        assert docs[3].meta["split_idx_start"] == text2.index(docs[3].content)
        # doc 4
        assert docs[4].content == "there is a fourth sentence."
        assert docs[4].meta["split_id"] == 2
        assert docs[4].meta["split_idx_start"] == text2.index(docs[4].content)

    def test_split_by_sentence(self):
        splitter = DocumentSplitter(split_by="sentence", split_length=1)
        text = "This is a text with some words. There is a second sentence. And there is a third sentence."
        result = splitter.run(documents=[Document(content=text)])
        docs = result["documents"]
        assert len(docs) == 3
        assert docs[0].content == "This is a text with some words."
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)
        assert docs[1].content == " There is a second sentence."
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)
        assert docs[2].content == " And there is a third sentence."
        assert docs[2].meta["split_id"] == 2
        assert docs[2].meta["split_idx_start"] == text.index(docs[2].content)

    def test_split_by_passage(self):
        splitter = DocumentSplitter(split_by="passage", split_length=1)
        text = "This is a text with some words. There is a second sentence.\n\nAnd there is a third sentence.\n\n And another passage."
        result = splitter.run(documents=[Document(content=text)])
        docs = result["documents"]
        assert len(docs) == 3
        assert docs[0].content == "This is a text with some words. There is a second sentence.\n\n"
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)
        assert docs[1].content == "And there is a third sentence.\n\n"
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)
        assert docs[2].content == " And another passage."
        assert docs[2].meta["split_id"] == 2
        assert docs[2].meta["split_idx_start"] == text.index(docs[2].content)

    def test_split_by_page(self):
        splitter = DocumentSplitter(split_by="page", split_length=1)
        text = "This is a text with some words. There is a second sentence.\f And there is a third sentence.\f And another passage."
        result = splitter.run(documents=[Document(content=text)])
        docs = result["documents"]
        assert len(docs) == 3
        assert docs[0].content == "This is a text with some words. There is a second sentence.\f"
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)
        assert docs[0].meta["page_number"] == 1
        assert docs[1].content == " And there is a third sentence.\f"
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)
        assert docs[1].meta["page_number"] == 2
        assert docs[2].content == " And another passage."
        assert docs[2].meta["split_id"] == 2
        assert docs[2].meta["split_idx_start"] == text.index(docs[2].content)
        assert docs[2].meta["page_number"] == 3

    def test_split_by_function(self):
        splitting_function = lambda s: s.split(".")
        splitter = DocumentSplitter(split_by="function", splitting_function=splitting_function)
        text = "This.Is.A.Test"
        result = splitter.run(documents=[Document(id="1", content=text, meta={"key": "value"})])
        docs = result["documents"]

        assert len(docs) == 4
        assert docs[0].content == "This"
        assert docs[0].meta == {"key": "value", "source_id": "1"}
        assert docs[1].content == "Is"
        assert docs[1].meta == {"key": "value", "source_id": "1"}
        assert docs[2].content == "A"
        assert docs[2].meta == {"key": "value", "source_id": "1"}
        assert docs[3].content == "Test"
        assert docs[3].meta == {"key": "value", "source_id": "1"}

        splitting_function = lambda s: re.split(r"[\s]{2,}", s)
        splitter = DocumentSplitter(split_by="function", splitting_function=splitting_function)
        text = "This       Is\n  A  Test"
        result = splitter.run(documents=[Document(id="1", content=text, meta={"key": "value"})])
        docs = result["documents"]
        assert len(docs) == 4
        assert docs[0].content == "This"
        assert docs[0].meta == {"key": "value", "source_id": "1"}
        assert docs[1].content == "Is"
        assert docs[1].meta == {"key": "value", "source_id": "1"}
        assert docs[2].content == "A"
        assert docs[2].meta == {"key": "value", "source_id": "1"}
        assert docs[3].content == "Test"
        assert docs[3].meta == {"key": "value", "source_id": "1"}

    def test_split_by_word_with_overlap(self):
        splitter = DocumentSplitter(split_by="word", split_length=10, split_overlap=2)
        text = "This is a text with some words. There is a second sentence. And there is a third sentence."
        result = splitter.run(documents=[Document(content=text)])
        docs = result["documents"]
        assert len(docs) == 2
        # doc 0
        assert docs[0].content == "This is a text with some words. There is a "
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)
        assert docs[0].meta["_split_overlap"][0]["range"] == (0, 5)
        assert docs[1].content[0:5] == "is a "
        # doc 1
        assert docs[1].content == "is a second sentence. And there is a third sentence."
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)
        assert docs[1].meta["_split_overlap"][0]["range"] == (38, 43)
        assert docs[0].content[38:43] == "is a "

    def test_split_by_line(self):
        splitter = DocumentSplitter(split_by="line", split_length=1)
        text = "This is a text with some words.\nThere is a second sentence.\nAnd there is a third sentence."
        result = splitter.run(documents=[Document(content=text)])
        docs = result["documents"]

        assert len(docs) == 3
        assert docs[0].content == "This is a text with some words.\n"
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)
        assert docs[1].content == "There is a second sentence.\n"
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)
        assert docs[2].content == "And there is a third sentence."
        assert docs[2].meta["split_id"] == 2
        assert docs[2].meta["split_idx_start"] == text.index(docs[2].content)

    def test_source_id_stored_in_metadata(self):
        splitter = DocumentSplitter(split_by="word", split_length=10)
        doc1 = Document(content="This is a text with some words.")
        doc2 = Document(content="This is a different text with some words.")
        result = splitter.run(documents=[doc1, doc2])
        assert result["documents"][0].meta["source_id"] == doc1.id
        assert result["documents"][1].meta["source_id"] == doc2.id

    def test_copy_metadata(self):
        splitter = DocumentSplitter(split_by="word", split_length=10)
        documents = [
            Document(content="Text.", meta={"name": "doc 0"}),
            Document(content="Text.", meta={"name": "doc 1"}),
        ]
        result = splitter.run(documents=documents)
        assert len(result["documents"]) == 2
        assert result["documents"][0].id != result["documents"][1].id
        for doc, split_doc in zip(documents, result["documents"]):
            assert doc.meta.items() <= split_doc.meta.items()
            assert split_doc.content == "Text."

    def test_add_page_number_to_metadata_with_no_overlap_word_split(self):
        splitter = DocumentSplitter(split_by="word", split_length=2)
        doc1 = Document(content="This is some text.\f This text is on another page.")
        doc2 = Document(content="This content has two.\f\f page brakes.")
        result = splitter.run(documents=[doc1, doc2])

        expected_pages = [1, 1, 2, 2, 2, 1, 1, 3]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_no_overlap_sentence_split(self):
        splitter = DocumentSplitter(split_by="sentence", split_length=1)
        doc1 = Document(content="This is some text.\f This text is on another page.")
        doc2 = Document(content="This content has two.\f\f page brakes.")
        result = splitter.run(documents=[doc1, doc2])

        expected_pages = [1, 1, 1, 1]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_no_overlap_passage_split(self):
        splitter = DocumentSplitter(split_by="passage", split_length=1)
        doc1 = Document(
            content="This is a text with some words.\f There is a second sentence.\n\nAnd there is a third sentence.\n\nAnd more passages.\n\n\f And another passage."
        )
        result = splitter.run(documents=[doc1])

        expected_pages = [1, 2, 2, 2]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_no_overlap_page_split(self):
        splitter = DocumentSplitter(split_by="page", split_length=1)
        doc1 = Document(
            content="This is a text with some words. There is a second sentence.\f And there is a third sentence.\f And another passage."
        )
        result = splitter.run(documents=[doc1])
        expected_pages = [1, 2, 3]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

        splitter = DocumentSplitter(split_by="page", split_length=2)
        doc1 = Document(
            content="This is a text with some words. There is a second sentence.\f And there is a third sentence.\f And another passage."
        )
        result = splitter.run(documents=[doc1])
        expected_pages = [1, 3]

        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_overlap_word_split(self):
        splitter = DocumentSplitter(split_by="word", split_length=3, split_overlap=1)
        doc1 = Document(content="This is some text. And\f this text is on another page.")
        doc2 = Document(content="This content has two.\f\f page brakes.")
        result = splitter.run(documents=[doc1, doc2])

        expected_pages = [1, 1, 1, 2, 2, 1, 1, 3]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_overlap_sentence_split(self):
        splitter = DocumentSplitter(split_by="sentence", split_length=2, split_overlap=1)
        doc1 = Document(content="This is some text. And this is more text.\f This text is on another page. End.")
        doc2 = Document(content="This content has two.\f\f page brakes. More text.")
        result = splitter.run(documents=[doc1, doc2])

        expected_pages = [1, 1, 1, 2, 1, 1]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_overlap_passage_split(self):
        splitter = DocumentSplitter(split_by="passage", split_length=2, split_overlap=1)
        doc1 = Document(
            content="This is a text with some words.\f There is a second sentence.\n\nAnd there is a third sentence.\n\nAnd more passages.\n\n\f And another passage."
        )
        result = splitter.run(documents=[doc1])

        expected_pages = [1, 2, 2]
        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_page_number_to_metadata_with_overlap_page_split(self):
        splitter = DocumentSplitter(split_by="page", split_length=2, split_overlap=1)
        doc1 = Document(
            content="This is a text with some words. There is a second sentence.\f And there is a third sentence.\f And another passage."
        )
        result = splitter.run(documents=[doc1])
        expected_pages = [1, 2, 3]

        for doc, p in zip(result["documents"], expected_pages):
            assert doc.meta["page_number"] == p

    def test_add_split_overlap_information(self):
        splitter = DocumentSplitter(split_length=10, split_overlap=5, split_by="word")
        text = "This is a text with some words. There is a second sentence. And a third sentence."
        doc = Document(content="This is a text with some words. There is a second sentence. And a third sentence.")
        docs = splitter.run(documents=[doc])["documents"]

        # check split_overlap is added to all the documents
        assert len(docs) == 3
        # doc 0
        assert docs[0].content == "This is a text with some words. There is a "
        assert docs[0].meta["split_id"] == 0
        assert docs[0].meta["split_idx_start"] == text.index(docs[0].content)  # 0
        assert docs[0].meta["_split_overlap"][0]["range"] == (0, 23)
        assert docs[1].content[0:23] == "some words. There is a "
        # doc 1
        assert docs[1].content == "some words. There is a second sentence. And a third "
        assert docs[1].meta["split_id"] == 1
        assert docs[1].meta["split_idx_start"] == text.index(docs[1].content)  # 20
        assert docs[1].meta["_split_overlap"][0]["range"] == (20, 43)
        assert docs[1].meta["_split_overlap"][1]["range"] == (0, 29)
        assert docs[0].content[20:43] == "some words. There is a "
        assert docs[2].content[0:29] == "second sentence. And a third "
        # doc 2
        assert docs[2].content == "second sentence. And a third sentence."
        assert docs[2].meta["split_id"] == 2
        assert docs[2].meta["split_idx_start"] == text.index(docs[2].content)  # 43
        assert docs[2].meta["_split_overlap"][0]["range"] == (23, 52)
        assert docs[1].content[23:52] == "second sentence. And a third "

        # reconstruct the original document content from the split documents
        assert doc.content == merge_documents(docs)

    def test_to_dict(self):
        """
        Test the to_dict method of the DocumentSplitter class.
        """
        splitter = DocumentSplitter(split_by="word", split_length=10, split_overlap=2, split_threshold=5)
        serialized = splitter.to_dict()

        assert serialized["type"] == "haystack.components.preprocessors.document_splitter.DocumentSplitter"
        assert serialized["init_parameters"]["split_by"] == "word"
        assert serialized["init_parameters"]["split_length"] == 10
        assert serialized["init_parameters"]["split_overlap"] == 2
        assert serialized["init_parameters"]["split_threshold"] == 5
        assert "splitting_function" not in serialized["init_parameters"]

    def test_to_dict_with_splitting_function(self):
        """
        Test the to_dict method of the DocumentSplitter class when a custom splitting function is provided.
        """

        splitter = DocumentSplitter(split_by="function", splitting_function=custom_split)
        serialized = splitter.to_dict()

        assert serialized["type"] == "haystack.components.preprocessors.document_splitter.DocumentSplitter"
        assert serialized["init_parameters"]["split_by"] == "function"
        assert "splitting_function" in serialized["init_parameters"]
        assert callable(deserialize_callable(serialized["init_parameters"]["splitting_function"]))

    def test_from_dict(self):
        """
        Test the from_dict class method of the DocumentSplitter class.
        """
        data = {
            "type": "haystack.components.preprocessors.document_splitter.DocumentSplitter",
            "init_parameters": {"split_by": "word", "split_length": 10, "split_overlap": 2, "split_threshold": 5},
        }
        splitter = DocumentSplitter.from_dict(data)

        assert splitter.split_by == "word"
        assert splitter.split_length == 10
        assert splitter.split_overlap == 2
        assert splitter.split_threshold == 5
        assert splitter.splitting_function is None

    def test_from_dict_with_splitting_function(self):
        """
        Test the from_dict class method of the DocumentSplitter class when a custom splitting function is provided.
        """

        def custom_split(text):
            return text.split(".")

        data = {
            "type": "haystack.components.preprocessors.document_splitter.DocumentSplitter",
            "init_parameters": {"split_by": "function", "splitting_function": serialize_callable(custom_split)},
        }
        splitter = DocumentSplitter.from_dict(data)

        assert splitter.split_by == "function"
        assert callable(splitter.splitting_function)
        assert splitter.splitting_function("a.b.c") == ["a", "b", "c"]

    def test_roundtrip_serialization(self):
        """
        Test the round-trip serialization of the DocumentSplitter class.
        """
        original_splitter = DocumentSplitter(split_by="word", split_length=10, split_overlap=2, split_threshold=5)
        serialized = original_splitter.to_dict()
        deserialized_splitter = DocumentSplitter.from_dict(serialized)

        assert original_splitter.split_by == deserialized_splitter.split_by
        assert original_splitter.split_length == deserialized_splitter.split_length
        assert original_splitter.split_overlap == deserialized_splitter.split_overlap
        assert original_splitter.split_threshold == deserialized_splitter.split_threshold

    def test_roundtrip_serialization_with_splitting_function(self):
        """
        Test the round-trip serialization of the DocumentSplitter class when a custom splitting function is provided.
        """

        original_splitter = DocumentSplitter(split_by="function", splitting_function=custom_split)
        serialized = original_splitter.to_dict()
        deserialized_splitter = DocumentSplitter.from_dict(serialized)

        assert original_splitter.split_by == deserialized_splitter.split_by
        assert callable(deserialized_splitter.splitting_function)
        assert deserialized_splitter.splitting_function("a.b.c") == ["a", "b", "c"]

    def test_run_empty_document(self):
        """
        Test if the component runs correctly with an empty document.
        """
        splitter = DocumentSplitter()
        doc = Document(content="")
        results = splitter.run([doc])
        assert results["documents"] == []

    def test_run_document_only_whitespaces(self):
        """
        Test if the component runs correctly with a document containing only whitespaces.
        """
        splitter = DocumentSplitter()
        doc = Document(content="  ")
        results = splitter.run([doc])
        assert results["documents"][0].content == "  "
