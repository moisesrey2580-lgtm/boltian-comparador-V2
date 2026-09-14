# Boltian Comparador V2

Versión revisada del comparador de Boltian.

## Flujo
1. El cliente sube una factura PDF/foto.
2. El servidor extrae consumo, días, potencia, importe y compañía.
3. El cliente confirma/corrige los datos.
4. El sistema compara las seis ofertas.
5. El cliente elige una oferta.
6. Se solicitan nombre, teléfono y email + consentimiento.
7. El servidor recalcula la oferta para evitar manipulaciones.
8. Se envía a `clientes@boltian.es` un email con:
   - datos del cliente
   - datos de la factura
   - oferta elegida
   - ahorro estimado
   - factura original adjunta

La factura se mantiene en memoria durante cada petición y no se guarda en disco de forma permanente.

## Variables de Railway
Configurar en Railway > servicio > Variables:

- SMTP_HOST
- SMTP_PORT
- SMTP_USER
- SMTP_PASSWORD
- SMTP_FROM
- SMTP_USE_SSL
- LEAD_TO=clientes@boltian.es

No pongas la contraseña en el código ni la subas a GitHub.

## Proveedor de correo
Usa los datos SMTP del proveedor donde tienes alojado `clientes@boltian.es`.
Normalmente:
- STARTTLS: puerto 587 y `SMTP_USE_SSL=false`
- SSL directo: puerto 465 y `SMTP_USE_SSL=true`

## Despliegue en Railway
1. Sube todos estos archivos a GitHub.
2. Railway > New Project > Deploy from GitHub repo.
3. Añade las variables anteriores.
4. Railway detectará el Dockerfile.
5. Settings > Networking > Generate Domain.
6. Prueba `/health`; debe devolver `{"status":"ok"}`.
7. Haz una prueba real y verifica que llega el email con la factura adjunta.

## Sitejet
Cuando la app funcione en Railway, enlázala desde la pestaña Comparador.
Si quieres incrustarla:
```html
<iframe
  src="https://TU-DOMINIO-RAILWAY"
  style="width:100%;min-height:1400px;border:0"
  loading="lazy">
</iframe>
```

## Pendiente antes de producción
- Revisar periódicamente precios/promociones de las 6 comercializadoras.
- Cambiar `/privacidad` por la URL real de política de privacidad si está en otro dominio/ruta.
- Probar el lector con facturas reales de cada comercializadora.
- Añadir rate limiting/WAF antes de campañas grandes.
