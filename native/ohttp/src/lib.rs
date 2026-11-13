//! Thin fixed-profile bindings. HPKE, AEAD and bHTTP framing stay upstream.
use std::io::Cursor;

use bhttp::{Message, Mode};
use ohttp::hpke::{Aead, Kdf, Kem};
use ohttp::{KeyConfig, SymmetricSuite};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

const MAX_BYTES: usize = 64 * 1024 * 1024;
type Headers = Vec<(String, String)>;
type Request<'py> = (String, String, String, Headers, Bound<'py, PyBytes>);
type Response<'py> = (u16, Headers, Bound<'py, PyBytes>);

fn invalid() -> PyErr {
    PyValueError::new_err("invalid encrypted message or consumed context")
}

fn bounded(data: &[u8]) -> PyResult<()> {
    if data.len() > MAX_BYTES {
        Err(invalid())
    } else {
        Ok(())
    }
}

// PyO3 enforces compiler-checked Send/Sync and exclusive mutable borrows.
// Python GC may finalize a context on another thread. Secrets are never
// printable/exported, and a failed operation still consumes the context.
#[pyclass]
struct ClientRequest(Option<ohttp::ClientRequest>);

#[pymethods]
impl ClientRequest {
    #[new]
    fn new(config: &[u8]) -> PyResult<Self> {
        // Exactly one X25519/HKDF-SHA256/AES-128-GCM configuration.
        if config.len() != 41 || config[1..3] != [0, 32] || config[35..] != [0, 4, 0, 1, 0, 1] {
            return Err(invalid());
        }
        let ctx = ohttp::ClientRequest::from_encoded_config(config).map_err(|_| invalid())?;
        Ok(Self(Some(ctx)))
    }

    fn encapsulate<'py>(
        &mut self,
        py: Python<'py>,
        data: &[u8],
    ) -> PyResult<(Bound<'py, PyBytes>, ClientResponse)> {
        let ctx = self.0.take().ok_or_else(invalid)?;
        bounded(data)?;
        let (encoded, response) = ctx.encapsulate(data).map_err(|_| invalid())?;
        Ok((PyBytes::new(py, &encoded), ClientResponse(Some(response))))
    }
}

#[pyclass]
struct ClientResponse(Option<ohttp::ClientResponse>);

#[pymethods]
impl ClientResponse {
    fn decapsulate<'py>(&mut self, py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        let ctx = self.0.take().ok_or_else(invalid)?;
        bounded(data)?;
        let decoded = ctx.decapsulate(data).map_err(|_| invalid())?;
        Ok(PyBytes::new(py, &decoded))
    }
}

#[pyclass]
struct Server(ohttp::Server);

#[pymethods]
impl Server {
    #[new]
    fn new(seed: &[u8], key_id: u8) -> PyResult<Self> {
        if seed.len() != 32 {
            return Err(invalid());
        }
        let config = KeyConfig::derive(
            key_id,
            Kem::X25519Sha256,
            vec![SymmetricSuite::new(Kdf::HkdfSha256, Aead::Aes128Gcm)],
            seed,
        )
        .map_err(|_| invalid())?;
        Ok(Self(ohttp::Server::new(config).map_err(|_| invalid())?))
    }

    fn config<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let config = self.0.config().encode().map_err(|_| invalid())?;
        Ok(PyBytes::new(py, &config))
    }

    fn decapsulate<'py>(
        &self,
        py: Python<'py>,
        data: &[u8],
    ) -> PyResult<(Bound<'py, PyBytes>, ServerResponse)> {
        bounded(data)?;
        let (decoded, response) = self.0.decapsulate(data).map_err(|_| invalid())?;
        Ok((PyBytes::new(py, &decoded), ServerResponse(Some(response))))
    }
}

#[pyclass]
struct ServerResponse(Option<ohttp::ServerResponse>);

#[pymethods]
impl ServerResponse {
    fn encapsulate<'py>(&mut self, py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        let ctx = self.0.take().ok_or_else(invalid)?;
        bounded(data)?;
        let encoded = ctx.encapsulate(data).map_err(|_| invalid())?;
        Ok(PyBytes::new(py, &encoded))
    }
}

fn field(name: &str, value: &str) -> PyResult<()> {
    if name.is_empty()
        || name.len() > 128
        || value.len() > 8192
        || !name.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || b"!#$%&'*+-.^_`|~".contains(&b)
        })
        || !value.bytes().all(|b| (32..=126).contains(&b))
    {
        return Err(invalid());
    }
    Ok(())
}

fn write<'py>(
    py: Python<'py>,
    mut message: Message,
    headers: Headers,
    body: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    bounded(body)?;
    if headers.len() > 64 {
        return Err(invalid());
    }
    for (name, value) in headers {
        field(&name, &value)?;
        message.put_header(name.into_bytes(), value.into_bytes());
    }
    message.write_content(body);
    let mut encoded = Vec::new();
    message
        .write_bhttp(Mode::KnownLength, &mut encoded)
        .map_err(|_| invalid())?;
    bounded(&encoded)?;
    Ok(PyBytes::new(py, &encoded))
}

#[pyfunction]
fn encode_request<'py>(
    py: Python<'py>,
    method: &str,
    authority: &str,
    path: &str,
    headers: Headers,
    body: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    if !matches!(method, "GET" | "POST")
        || authority.is_empty()
        || authority.len() > 512
        || !authority.bytes().all(|b| (33..=126).contains(&b))
        || !path.starts_with('/')
        || path.len() > 4096
        || !path.bytes().all(|b| (33..=126).contains(&b))
    {
        return Err(invalid());
    }
    write(
        py,
        Message::request(
            method.as_bytes().to_vec(),
            b"https".to_vec(),
            authority.as_bytes().to_vec(),
            path.as_bytes().to_vec(),
        ),
        headers,
        body,
    )
}

#[pyfunction]
fn encode_response<'py>(
    py: Python<'py>,
    status: u16,
    headers: Headers,
    body: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    if !(200..=599).contains(&status) {
        return Err(invalid());
    }
    write(
        py,
        Message::response(status.try_into().map_err(|_| invalid())?),
        headers,
        body,
    )
}

fn read(data: &[u8], request: bool) -> PyResult<Message> {
    bounded(data)?;
    // This profile uses known-length bHTTP only; stream/informational/trailers
    // are not exposed to the Broker dispatcher.
    if data.first() != Some(&if request { 0 } else { 1 }) {
        return Err(invalid());
    }
    let mut cursor = Cursor::new(data);
    let message = Message::read_bhttp(&mut cursor).map_err(|_| invalid())?;
    if message.control().is_request() != request
        || !message.informational().is_empty()
        || !message.trailer().fields().is_empty()
        || data[cursor.position() as usize..].iter().any(|b| *b != 0)
    {
        return Err(invalid());
    }
    Ok(message)
}

fn string(value: &[u8]) -> PyResult<String> {
    std::str::from_utf8(value)
        .map(str::to_owned)
        .map_err(|_| invalid())
}

fn headers(message: &Message) -> PyResult<Headers> {
    if message.header().fields().len() > 64 {
        return Err(invalid());
    }
    message
        .header()
        .iter()
        .map(|f| {
            let name = string(f.name())?;
            let value = string(f.value())?;
            field(&name, &value)?;
            Ok((name, value))
        })
        .collect()
}

#[pyfunction]
fn decode_request<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Request<'py>> {
    let message = read(data, true)?;
    let control = message.control();
    if control.scheme() != Some(b"https") {
        return Err(invalid());
    }
    Ok((
        string(control.method().ok_or_else(invalid)?)?,
        string(control.authority().ok_or_else(invalid)?)?,
        string(control.path().ok_or_else(invalid)?)?,
        headers(&message)?,
        PyBytes::new(py, message.content()),
    ))
}

#[pyfunction]
fn decode_response<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Response<'py>> {
    let message = read(data, false)?;
    let status: u16 = message.control().status().ok_or_else(invalid)?.into();
    if !(200..=599).contains(&status) {
        return Err(invalid());
    }
    Ok((
        status,
        headers(&message)?,
        PyBytes::new(py, message.content()),
    ))
}

#[pymodule]
fn quota_broker_ohttp(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ClientRequest>()?;
    m.add_class::<ClientResponse>()?;
    m.add_class::<Server>()?;
    m.add_class::<ServerResponse>()?;
    m.add_function(wrap_pyfunction!(encode_request, m)?)?;
    m.add_function(wrap_pyfunction!(decode_request, m)?)?;
    m.add_function(wrap_pyfunction!(encode_response, m)?)?;
    m.add_function(wrap_pyfunction!(decode_response, m)?)?;
    Ok(())
}
