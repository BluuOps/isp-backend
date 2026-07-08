from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import conflict
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import Customer, Organization, User
from app.schemas import CustomerCreate, CustomerResponse, CustomerUpdate
from app.services.limits import enforce_limit


router = APIRouter(prefix="/customers", tags=["CRM Customers"])


def customer_to_response(customer: Customer, organization: OrganizationContext) -> CustomerResponse:
    return CustomerResponse(
        id=customer.id,
        tenantId=organization.slug,
        name=customer.name,
        customerType=customer.customer_type,
        email=customer.email,
        phone=customer.phone,
        address=customer.address,
        location={"lat": customer.latitude, "lng": customer.longitude},
        mstId=customer.mst_id,
        splitterPort=customer.splitter_port,
        fibreCoreId=customer.fibre_core_id,
        onuSerial=customer.onu_serial,
        oltName=customer.olt_name,
        ponPort=customer.pon_port,
        rxSignal=customer.rx_signal,
        txSignal=customer.tx_signal,
        accountStatus=customer.account_status,
        online=customer.online,
    )


def apply_customer_payload(
    customer: Customer,
    payload: CustomerCreate | CustomerUpdate,
    organization: OrganizationContext,
) -> None:
    customer.tenant_id = organization.slug
    customer.name = payload.name
    customer.customer_type = payload.customerType
    customer.email = payload.email
    customer.phone = payload.phone
    customer.address = payload.address
    customer.latitude = payload.location.lat
    customer.longitude = payload.location.lng
    customer.mst_id = payload.mstId
    customer.splitter_port = payload.splitterPort
    customer.fibre_core_id = payload.fibreCoreId
    customer.onu_serial = payload.onuSerial
    customer.olt_name = payload.oltName
    customer.pon_port = payload.ponPort
    customer.rx_signal = payload.rxSignal
    customer.tx_signal = payload.txSignal
    customer.account_status = payload.accountStatus
    customer.online = payload.online


@router.get("", response_model=List[CustomerResponse])
def list_customers(
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> list[CustomerResponse]:
    customers = (
        db.query(Customer)
        .filter(Customer.organization_id == organization.id)
        .order_by(Customer.created_at.desc(), Customer.name.asc())
        .all()
    )
    return [customer_to_response(customer, organization) for customer in customers]


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> CustomerResponse:
    organization_row = db.query(Organization).filter(Organization.id == organization.id).one()
    enforce_limit(db, organization_row, "customers")
    existing = (
        db.query(Customer)
        .filter(Customer.id == payload.id, Customer.organization_id == organization.id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Customer already exists")

    customer = Customer(id=payload.id, organization_id=organization.id)
    apply_customer_payload(customer, payload, organization)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer_to_response(customer, organization)


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> CustomerResponse:
    customer = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.organization_id == organization.id)
        .first()
    )
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer_to_response(customer, organization)


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: str,
    payload: CustomerUpdate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> CustomerResponse:
    customer = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.organization_id == organization.id)
        .first()
    )
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    apply_customer_payload(customer, payload, organization)
    db.commit()
    db.refresh(customer)
    return customer_to_response(customer, organization)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> None:
    customer = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.organization_id == organization.id)
        .first()
    )
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    linked_user = (
        db.query(User)
        .filter(User.customer_id == customer.id, User.organization_id == organization.id)
        .first()
    )
    if linked_user:
        raise conflict(
            "customer_has_subscribers",
            "Remove or reassign linked PPPoE subscribers before deleting this customer.",
        )

    try:
        db.delete(customer)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "customer_has_linked_records",
            "Remove linked subscribers or related records before deleting this customer.",
        ) from exc
